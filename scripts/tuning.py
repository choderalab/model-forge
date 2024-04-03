import torch
from ray import tune, air
from ray.tune.schedulers import ASHAScheduler

from modelforge.potential import NeuralNetworkPotentialFactory
from modelforge.dataset.qm9 import QM9Dataset
from modelforge.dataset.dataset import TorchDataModule
from modelforge.dataset.utils import RandomRecordSplittingStrategy
from pytorch_lightning.loggers import TensorBoardLogger

# set up tensor board logger
logger = TensorBoardLogger("tb_logs", name="training")

# Set up and prepare dataset
data = QM9Dataset()
dataset = TorchDataModule(
    data, batch_size=512, splitting_strategy=RandomRecordSplittingStrategy()
)

dataset.prepare_data(remove_self_energies=True, normalize=False)

# Set up model
model = NeuralNetworkPotentialFactory.create_nnp("training", "ANI2x")
model = model.to(torch.float32)

model.tune_with_ray(
    train_dataloader=dataset.train_dataloader(),
    val_dataloader=dataset.val_dataloader(),
)
