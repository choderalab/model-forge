# This is an example script that trains the PaiNN model on the .
from lightning import Trainer
import torch
from modelforge.potential.schnet import SchNET
from modelforge.potential.ani import ANI2x

from modelforge.dataset.qm9 import QM9Dataset
from modelforge.dataset.dataset import TorchDataModule
from modelforge.dataset.utils import FirstComeFirstServeSplittingStrategy

# Set up dataset
data = QM9Dataset(for_unit_testing=False)
dataset = TorchDataModule(
    data, batch_size=128, split=FirstComeFirstServeSplittingStrategy()
)

dataset.prepare_data(remove_self_energies=True, normalize=True)

# Set up model
model = ANI2x()
model = model.to(torch.float32)

print(model)

# set up traininer

from lightning.pytorch.callbacks.early_stopping import EarlyStopping

trainer = Trainer(
    max_epochs=10_000,
    num_nodes=1,
    accelerator="cpu",
    # devices=[0],
    # callbacks=[
    #    EarlyStopping(monitor="val_loss", mode="min", patience=3, min_delta=0.001)
    # ],
)


# set scaling and ase values

dataset.dataset_statistics["scaling_stddev"] = 1
model.dataset_statistics = dataset.dataset_statistics

# Run training loop and validate
trainer.fit(model, dataset.train_dataloader(), dataset.val_dataloader())
