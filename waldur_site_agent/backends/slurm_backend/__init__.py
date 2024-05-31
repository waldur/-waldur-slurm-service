"""SLURM backend module."""

import os
from pathlib import Path

import yaml

SLURM_ALLOCATION_REGEX = "a-zA-Z0-9-_"
SLURM_ALLOCATION_NAME_MAX_LEN = int(os.environ.get("SLURM_ALLOCATION_NAME_MAX_LEN", 34))

SLURM_CUSTOMER_PREFIX = os.environ.get("SLURM_CUSTOMER_PREFIX", "hpc_")
SLURM_PROJECT_PREFIX = os.environ.get("SLURM_PROJECT_PREFIX", "hpc_")
SLURM_ALLOCATION_PREFIX = os.environ.get("SLURM_ALLOCATION_PREFIX", "hpc_")

SLURM_TRES_CONFIG_PATH = os.environ.get("SLURM_TRES_CONFIG_PATH", "config-components.yaml")

with Path(SLURM_TRES_CONFIG_PATH).open(encoding="UTF-8") as stream:
    tres_config = yaml.safe_load(stream)
    SLURM_TRES = tres_config


SLURM_DEFAULT_ACCOUNT = os.environ.get("SLURM_DEFAULT_ACCOUNT", "waldur")
