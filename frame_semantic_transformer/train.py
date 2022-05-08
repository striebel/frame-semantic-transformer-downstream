from __future__ import annotations
import os
from typing import Any, Optional
import numpy as np
import pytorch_lightning as pl
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AdamW, T5ForConditionalGeneration, T5Tokenizer
from pytorch_lightning.callbacks.progress import TQDMProgressBar
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks.base import Callback

from frame_semantic_transformer.data.TaskSampleDataset import TaskSampleDataset
from frame_semantic_transformer.data.load_framenet_samples import (
    load_sesame_train_samples,
    load_sesame_test_samples,
    load_sesame_dev_samples,
)

DEFAULT_NUM_WORKERS = os.cpu_count() or 2


class TrainDataModule(pl.LightningDataModule):
    """
    Based on https://github.com/Shivanandroy/simpleT5/blob/main/simplet5/simplet5.py
    """

    batch_size: int
    train_dataset: Dataset[Any]
    val_dataset: Dataset[Any]
    test_dataset: Optional[Dataset[Any]]
    num_workers: int

    def __init__(
        self,
        train_dataset: Dataset[Any],
        val_dataset: Dataset[Any],
        test_dataset: Optional[Dataset[Any]] = None,
        batch_size: int = 8,
        num_workers: int = DEFAULT_NUM_WORKERS,
    ):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers

        self.train_dataset = train_dataset
        self.test_dataset = test_dataset
        self.val_dataset = val_dataset

    def train_dataloader(self) -> DataLoader[Any]:
        dataloader: Any = DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=True,
        )
        return dataloader

    def val_dataloader(self) -> DataLoader[Any]:
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=False,
        )

    def test_dataloader(self) -> DataLoader[Any]:
        dataset = self.test_dataset if self.test_dataset else self.val_dataset
        return DataLoader(
            dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=False,
        )


class TrainingModelWrapper(pl.LightningModule):
    """
    Based on https://github.com/Shivanandroy/simpleT5/blob/main/simplet5/simplet5.py
    """

    lr: float
    model: T5ForConditionalGeneration
    tokenizer: T5Tokenizer
    trainer: pl.Trainer
    output_dir: str
    save_only_last_epoch: bool

    def __init__(
        self,
        model: T5ForConditionalGeneration,
        tokenizer: T5Tokenizer,
        lr: float = 1e-4,
        output_dir: str = "outputs",
        save_only_last_epoch: bool = False,
    ):
        super().__init__()
        self.lr = lr
        self.model = model
        self.tokenizer = tokenizer
        self.output_dir = output_dir
        self.save_only_last_epoch = save_only_last_epoch

    def forward(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return self.model(*args, **kwargs)

    def _step(self, batch: Any) -> Any:
        output = self(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            labels=batch["labels"],
        )
        return output.loss

    def training_step(self, batch: Any, _batch_idx: int) -> Any:  # type: ignore
        loss = self._step(batch)
        self.log(
            "train_loss", loss, prog_bar=True, logger=True, on_epoch=True, on_step=True
        )
        return loss

    def validation_step(self, batch: Any, _batch_idx: int) -> Any:  # type: ignore
        loss = self._step(batch)
        self.log(
            "val_loss", loss, prog_bar=True, logger=True, on_epoch=True, on_step=True
        )
        return loss

    def test_step(self, batch: Any, _batch_idx: int) -> Any:  # type: ignore
        loss = self._step(batch)
        self.log("test_loss", loss, prog_bar=True, logger=True)
        return loss

    def configure_optimizers(self) -> AdamW:
        return AdamW(self.parameters(), lr=self.lr)

    def training_epoch_end(self, training_step_outputs: list[Any]) -> None:
        """save tokenizer and model on epoch end"""
        self.average_training_loss = np.round(
            torch.mean(torch.stack([x["loss"] for x in training_step_outputs])).item(),
            4,
        )
        path = f"{self.output_dir}/epoch-{self.current_epoch}-train-loss-{str(self.average_training_loss)}-val-loss-{str(self.average_validation_loss)}"
        if (
            not self.save_only_last_epoch
            or self.current_epoch == self.trainer.max_epochs - 1
        ):
            self.tokenizer.save_pretrained(path)
            self.model.save_pretrained(path)

    def validation_epoch_end(self, validation_step_outputs: list[Any]) -> None:
        losses = [x.cpu() for x in validation_step_outputs]
        self.average_validation_loss = np.round(
            torch.mean(torch.stack(losses)).item(),
            4,
        )


def train(
    base_model: str = "t5-base",
    batch_size: int = 8,
    max_epochs: int = 5,
    use_gpu: bool = torch.cuda.is_available(),
    output_dir: str = "outputs",
    early_stopping_patience_epochs: int = 0,  # 0 to disable early stopping feature
    precision: int = 32,
    lr: float = 1e-4,
    num_workers: int = DEFAULT_NUM_WORKERS,
    save_only_last_epoch: bool = False,
) -> tuple[T5ForConditionalGeneration, T5Tokenizer]:
    device = torch.device("cuda" if use_gpu else "cpu")
    model = T5ForConditionalGeneration.from_pretrained(base_model).to(device)
    tokenizer = T5Tokenizer.from_pretrained(base_model)

    train_dataset = TaskSampleDataset(load_sesame_train_samples(), tokenizer)
    val_dataset = TaskSampleDataset(load_sesame_dev_samples(), tokenizer)
    test_dataset = TaskSampleDataset(load_sesame_test_samples(), tokenizer)

    data_module = TrainDataModule(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        test_dataset=test_dataset,
        batch_size=batch_size,
        num_workers=num_workers,
    )

    model_wrapper = TrainingModelWrapper(
        model,
        tokenizer,
        lr=lr,
        output_dir=output_dir,
        save_only_last_epoch=save_only_last_epoch,
    )

    # add callbacks
    callbacks: list[Callback] = [TQDMProgressBar(refresh_rate=5)]

    if early_stopping_patience_epochs > 0:
        early_stop_callback = EarlyStopping(
            monitor="val_loss",
            min_delta=0.00,
            patience=early_stopping_patience_epochs,
            verbose=True,
            mode="min",
        )
        callbacks.append(early_stop_callback)

    # prepare trainer
    trainer = pl.Trainer(
        callbacks=callbacks,
        max_epochs=max_epochs,
        gpus=1 if use_gpu else 0,
        precision=precision,
        log_every_n_steps=1,
    )

    # fit trainer
    trainer.fit(model_wrapper, data_module)

    return model, tokenizer
