import json
import os
import tempfile
import warnings
from argparse import Namespace
from pathlib import Path
from typing import Dict

import joblib
import mlflow
import optuna
import pandas as pd
import typer
from numpyencoder import NumpyEncoder
from optuna.integration.mlflow import MLflowCallback

from config import config
from config.config import logger
from src.pipelines import data, predict, train
from src.utils import utils
from prefect import flow, task
from prefect.runtime import flow_run, task_run
import datetime

warnings.filterwarnings("ignore")

# Initialize Typer CLI app
app = typer.Typer()

def generate_task_name():
    flow_name = flow_run.flow_name
    task_name = task_run.task_name
    date = datetime.datetime.utcnow()
    return f"[{date:%x}][{flow_name}]{task_name}"

@app.command()
@task(task_run_name=generate_task_name,)
def elt_data():
    """Extract, load and transform our data assets."""
    # Extract + Load
    projects = pd.read_csv(config.PROJECTS_URL)
    tags = pd.read_csv(config.TAGS_URL)
    projects.to_csv(Path(config.DATA_DIR, "projects.csv"), index=False)
    tags.to_csv(Path(config.DATA_DIR, "tags.csv"), index=False)

    # Transform
    df = pd.merge(projects, tags, on="id")
    df = df[df.tag.notnull()]  # drop rows w/ no tag
    
    # last 100 sample for testing
    test_df = df[-300:]
    train_df = df[:-300]
    
    train_df.to_csv(Path(config.DATA_DIR, "labeled_projects.csv"), index=False)
    test_df.to_csv(Path(config.DATA_DIR, "test_labeled_projects.csv"), index=False)

    logger.info("✅ Saved data!")


@app.command()
@task(task_run_name=generate_task_name,)
def train_model(
    args_fp: str = "config/args.json",
    experiment_name: str = "baselines",
    run_name: str = "sgd",
    test_run: bool = False,
) -> None:
    """Train a model given arguments.

    Args:
        args_fp (str): location of args.
        experiment_name (str): name of experiment.
        run_name (str): name of specific run in experiment.
        test_run (bool, optional): If True, artifacts will not be saved. Defaults to False.
    """
    # Load labeled data
    df = pd.read_csv(Path(config.DATA_DIR, "labeled_projects.csv"))

    # Train
    args = Namespace(**utils.load_dict(filepath=args_fp))
    mlflow.set_experiment(experiment_name=experiment_name)
    with mlflow.start_run(run_name=run_name):
        run_id = mlflow.active_run().info.run_id
        logger.info(f"Run ID: {run_id}")
        artifacts = train.train(df=df, args=args)
        performance = artifacts["performance"]
        logger.info(json.dumps(performance, indent=2))

        # Log metrics and parameters
        performance = artifacts["performance"]
        mlflow.log_metrics({"precision": performance["overall"]["precision"]})
        mlflow.log_metrics({"recall": performance["overall"]["recall"]})
        mlflow.log_metrics({"f1": performance["overall"]["f1"]})
        mlflow.log_params(vars(artifacts["args"]))

        # Log artifacts
        with tempfile.TemporaryDirectory() as dp:
            utils.save_dict(
                vars(artifacts["args"]),
                Path(dp, "args.json"),
                cls=NumpyEncoder,
            )
            artifacts["label_encoder"].save(Path(dp, "label_encoder.json"))
            joblib.dump(artifacts["vectorizer"], Path(dp, "vectorizer.pkl"))
            # joblib.dump(artifacts["model"], Path(dp, "model.pkl"))
            utils.save_dict(performance, Path(dp, "performance.json"))
            mlflow.log_artifacts(dp, artifact_path="artifacts")
            mlflow.sklearn.log_model(artifacts["model"], "model")

    # Save to config
    if not test_run:  # pragma: no cover, actual run
        open(Path(config.CONFIG_DIR, "run_id.txt"), "w").write(run_id)
        utils.save_dict(
            performance, Path(config.CONFIG_DIR, "performance.json")
        )


@app.command()
@task(task_run_name=generate_task_name,)
def optimize(
    args_fp: str = "config/args.json",
    study_name: str = "optimization",
    num_trials: int = 20,
) -> None:
    """Optimize hyperparameters.

    Args:
        args_fp (str): location of args.
        study_name (str): name of optimization study.
        num_trials (int): number of trials to run in study.
    """
    # Load labeled data
    df = pd.read_csv(Path(config.DATA_DIR, "labeled_projects.csv"))

    # Optimize
    args = Namespace(**utils.load_dict(filepath=args_fp))
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=5)
    study = optuna.create_study(
        study_name=study_name, direction="maximize", pruner=pruner
    )
    mlflow_callback = MLflowCallback(
        tracking_uri=mlflow.get_tracking_uri(), metric_name="f1"
    )
    study.optimize(
        lambda trial: train.objective(args, df, trial),
        n_trials=num_trials,
        callbacks=[mlflow_callback],
    )

    # Best trial
    trials_df = study.trials_dataframe()
    trials_df = trials_df.sort_values(["user_attrs_f1"], ascending=False)
    args = {**args.__dict__, **study.best_trial.params}
    utils.save_dict(d=args, filepath=args_fp, cls=NumpyEncoder)
    logger.info(f"\nBest value (f1): {study.best_trial.value}")
    logger.info(
        f"Best hyperparameters: {json.dumps(study.best_trial.params, indent=2)}"
    )


def load_artifacts(run_id: str = None) -> Dict:
    """Load artifacts for a given run_id.

    Args:
        run_id (str): id of run to load artifacts from.

    Returns:
        Dict: run's artifacts.
    """
    if not run_id:
        run_id = open(Path(config.CONFIG_DIR, "run_id.txt")).read()

    client = mlflow.tracking.MlflowClient()
    with tempfile.TemporaryDirectory() as dp:
        client.download_artifacts(run_id, "", dp)
        args = Namespace(
            **utils.load_dict(filepath=Path(dp, "artifacts", "args.json"))
        )
        vectorizer = joblib.load(Path(dp, "artifacts", "vectorizer.pkl"))
        label_encoder = data.LabelEncoder.load(
            fp=Path(dp, "artifacts", "label_encoder.json")
        )
        performance = utils.load_dict(
            filepath=Path(dp, "artifacts", "performance.json")
        )
        model = joblib.load(Path(dp, "model", "model.pkl"))

    return {
        "args": args,
        "label_encoder": label_encoder,
        "vectorizer": vectorizer,
        "model": model,
        "performance": performance,
    }


@app.command()
def predict_tag(text: str = "", run_id: str = None):
    """Predict tag for text.

    Args:
        text (str): input text to predict label for.
        run_id (str, optional): run id to load artifacts for prediction. Defaults to None.
    """
    if not run_id:
        run_id = open(Path(config.CONFIG_DIR, "run_id.txt")).read()
    artifacts = load_artifacts(run_id=run_id)
    prediction = predict.predict(texts=[text], artifacts=artifacts)
    logger.info(json.dumps(prediction, indent=2))
    return prediction

@app.command()
@flow(name="elt-data-pipeline")
def elt_data_pipeline():
    """Pipeline to extract, load, and transform data."""
    elt_data()

@app.command()
@flow(name="train model pipeline")
def train_model_pipeline(args_fp: str = "config/args.json", experiment_name: str = "baselines", run_name: str = "sgd"):
    """Pipeline to train model."""
    elt_data()
    optimize(args_fp, study_name = "optimization", num_trials = 20)
    train_model(args_fp, experiment_name, run_name)

if __name__ == "__main__":
    app()  # pragma: no cover, live app
