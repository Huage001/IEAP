from torch.utils.data import DataLoader
import torch
import lightning as L
import yaml
import os
import time
import re

from datasets import load_dataset

from .data import ImageConditionDataset, Subject200KDataset, CartoonDataset, SceneDataset
from .model import OminiModel
from .callbacks import TrainingCallback
import safetensors.torch
from peft import PeftModel

import os
from PIL import Image
import pandas as pd
from torch.utils.data import Dataset

from torchvision import transforms
from torch.utils.data import DataLoader

class LocalSubjectsDataset(Dataset):
    def __init__(self, csv_file, image_dir, transform=None):
        self.data = pd.read_csv(csv_file)
        self.image_dir = image_dir
        self.transform = transform
        self.features = {
            'imageA': 'PIL.Image',
            'prompt': 'str',
            'imageB': 'PIL.Image'
        }

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        # 获取图片A、描述和图片B的文件名
        imgA_value = self.data.iloc[idx]['imageA']
        if isinstance(imgA_value, pd.Series):
            imgA_value = imgA_value.values[0]  
        imgA_name = os.path.join(self.image_dir, str(imgA_value)) 

        prompt = self.data.iloc[idx]['prompt']
        imgB_value = self.data.iloc[idx]['imageB']
        if isinstance(imgB_value, pd.Series):
            imgB_value = imgB_value.values[0]
        imgB_name = os.path.join(self.image_dir, str(imgB_value))

        imageA = Image.open(imgA_name).convert("RGB")
        imageB = Image.open(imgB_name).convert("RGB")

        if self.transform:
            imageA = self.transform(imageA)
            imageB = self.transform(imageB)

        sample = {'imageA': imageA, 'prompt': prompt, 'imageB': imageB}
        return sample
    
transform = transforms.Compose([
    transforms.Resize((600, 600)),
    # transforms.ToTensor(),
])


def get_rank():
    try:
        rank = int(os.environ.get("LOCAL_RANK"))
    except:
        rank = 0
    return rank


def get_config():
    config_path = os.environ.get("XFL_CONFIG")
    assert config_path is not None, "Please set the XFL_CONFIG environment variable"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config


def init_wandb(wandb_config, run_name):
    import wandb
    wandb.init(
            project=wandb_config["project"],
            name=run_name,
            config={},
        )


def main():
    # Initialize
    is_main_process, rank = get_rank() == 0, get_rank()
    torch.cuda.set_device(rank)
    config = get_config()
    training_config = config["train"]
    run_name = time.strftime("%Y%m%d-%H%M%S")

    # Initialize WanDB
    wandb_config = training_config.get("wandb", None)
    if wandb_config is not None and is_main_process:
        init_wandb(wandb_config, run_name)

    print("Rank:", rank)
    if is_main_process:
        print("Config:", config)

    # Initialize dataset and dataloader
    if training_config["dataset"]["type"] == "scene":
        dataset = LocalSubjectsDataset(csv_file='csv_path', image_dir='images_path', transform=transform)
        data_valid = dataset
        print(data_valid.features)
        print(len(data_valid))
        print(training_config["dataset"])
        dataset = SceneDataset(
            data_valid,
            condition_size=training_config["dataset"]["condition_size"],
            target_size=training_config["dataset"]["target_size"],
            image_size=training_config["dataset"]["image_size"],
            padding=training_config["dataset"]["padding"],
            condition_type=training_config["condition_type"],
            drop_text_prob=training_config["dataset"]["drop_text_prob"],
            drop_image_prob=training_config["dataset"]["drop_image_prob"],
        )
    elif training_config["dataset"]["type"] == "img":
        # Load dataset text-to-image-2M
        dataset = load_dataset(
            "webdataset",
            data_files={"train": training_config["dataset"]["urls"]},
            split="train",
            cache_dir="cache/t2i2m",
            num_proc=32,
        )
        dataset = ImageConditionDataset(
            dataset,
            condition_size=training_config["dataset"]["condition_size"],
            target_size=training_config["dataset"]["target_size"],
            condition_type=training_config["condition_type"],
            drop_text_prob=training_config["dataset"]["drop_text_prob"],
            drop_image_prob=training_config["dataset"]["drop_image_prob"],
            position_scale=training_config["dataset"].get("position_scale", 1.0),
        )
    elif training_config["dataset"]["type"] == "cartoon":
        dataset = load_dataset("saquiboye/oye-cartoon", split="train")
        dataset = CartoonDataset(
            dataset,
            condition_size=training_config["dataset"]["condition_size"],
            target_size=training_config["dataset"]["target_size"],
            image_size=training_config["dataset"]["image_size"],
            padding=training_config["dataset"]["padding"],
            condition_type=training_config["condition_type"],
            drop_text_prob=training_config["dataset"]["drop_text_prob"],
            drop_image_prob=training_config["dataset"]["drop_image_prob"],
        )
    elif training_config["dataset"]["type"] == "scene":
        dataset = dataset
    else:
        raise NotImplementedError

    print("Dataset length:", len(dataset))
    train_loader = DataLoader(
        dataset,
        batch_size=training_config["batch_size"],
        shuffle=True,
        num_workers=training_config["dataloader_workers"],
    )
    print("Trainloader generated.")

    # Initialize model
    trainable_model = OminiModel(
        flux_pipe_id=config["flux_path"],
        lora_config=training_config["lora_config"],
        device=f"cuda",
        dtype=getattr(torch, config["dtype"]),
        optimizer_config=training_config["optimizer"],
        model_config=config.get("model", {}),
        gradient_checkpointing=training_config.get("gradient_checkpointing", False),
    )

    training_callbacks = (
        [TrainingCallback(run_name, training_config=training_config)]
        if is_main_process
        else []
    )

    # Initialize trainer
    trainer = L.Trainer(
        accumulate_grad_batches=training_config["accumulate_grad_batches"],
        callbacks=training_callbacks,
        enable_checkpointing=False,
        enable_progress_bar=False,
        logger=False,
        max_steps=training_config.get("max_steps", -1),
        max_epochs=training_config.get("max_epochs", -1),
        gradient_clip_val=training_config.get("gradient_clip_val", 0.5),
    )

    setattr(trainer, "training_config", training_config)

    # Save config
    save_path = training_config.get("save_path", "./output")
    if is_main_process:
        os.makedirs(f"{save_path}/{run_name}")
        with open(f"{save_path}/{run_name}/config.yaml", "w") as f:
            yaml.dump(config, f)

    # Start training
    trainer.fit(trainable_model, train_loader)


if __name__ == "__main__":
    main()
