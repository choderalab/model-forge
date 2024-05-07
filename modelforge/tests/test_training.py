import os
import pytest

import platform

ON_MACOS = platform.system() == "Darwin"

IN_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"


@pytest.mark.skipif(ON_MACOS, reason="Skipping this test on MacOS GitHub Actions")
def test_train_with_lightning(train_model, initialized_QM9_dataset):
    """
    Test the forward pass for a given model and dataset.

    Parameters
    ----------
    dataset : Type[BaseNNP]
        The dataset class to be used in the test.
    model : str
        The model to be used in the test.
    """

    from lightning import Trainer
    import torch
    from modelforge.train.training import TrainingAdapter

    # Initialize PyTorch Lightning Trainer
    trainer = Trainer(max_epochs=2)

    # Move model to the appropriate dtype and device
    model = train_model.to(torch.float32)
    # Run training loop and validate
    trainer.fit(
        model,
        initialized_QM9_dataset.train_dataloader(),
        initialized_QM9_dataset.val_dataloader(),
    )
    # save checkpoint
    trainer.save_checkpoint("test.chp")
    model = TrainingAdapter.load_from_checkpoint("test.chp")
    assert type(model) is not None


@pytest.mark.skipif(ON_MACOS, reason="Skipping this test on MacOS GitHub Actions")
def test_hypterparameter_tuning_with_ray(train_model, initialized_QM9_dataset):

    train_model.tune_with_ray(
        train_dataloader=initialized_QM9_dataset.train_dataloader(),
        val_dataloader=initialized_QM9_dataset.val_dataloader(),
        number_of_ray_workers=1,
        number_of_epochs=1,
        number_of_samples=1,
    )
