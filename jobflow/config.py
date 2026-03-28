from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = "config.yaml"


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

    # Resolve paths relative to config file location
    base = config_path.parent
    config["_base"] = base
    config["output_dir"] = base / config["output_dir"]
    config["csv_path"] = base / config["csv_path"]
    config["resume_prompt"] = base / config["resume_prompt"]

    if "job_boards" in config:
        config["job_boards"] = base / config["job_boards"]

    for variant, rel_path in config["resumes"].items():
        config["resumes"][variant] = base / rel_path

    return config
