from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = "config/config.yaml"


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Run 'jobflow init' first."
        )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    required = ["resumes", "output_dir", "csv_path", "resume_prompt"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    # Resolve paths relative to project root (parent of config dir)
    root = config_path.parent.parent
    config["_root"] = root
    config["output_dir"] = root / config["output_dir"]
    config["csv_path"] = root / config["csv_path"]
    config["resume_prompt"] = root / config["resume_prompt"]

    if "job_boards" in config:
        config["job_boards"] = root / config["job_boards"]

    for variant, rel_path in config["resumes"].items():
        config["resumes"][variant] = root / rel_path

    return config
