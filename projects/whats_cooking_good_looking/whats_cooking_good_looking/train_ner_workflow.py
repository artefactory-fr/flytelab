import glob
import json
import pickle
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import List

import spacy
from flytekit import Resources, dynamic, task, workflow
from spacy.language import Language
from spacy.training import Example
from spacy.util import compounding, minibatch
from utils import download_bytes_from_gcs, download_from_gcs, load_config, upload_to_gcs

SPACY_MODEL = {"en": "en_core_web_sm"}

CACHE_VERSION = "2.2"
request_resources = Resources(cpu="1", mem="500Mi", storage="500Mi")
limit_resources = Resources(cpu="2", mem="1000Mi", storage="1000Mi")

THRESHOLD_ACCURACY = 0.7

@task
def retrieve_train_data_path(bucket_name: str, train_data_gcs_folder: str) -> List[str]:
    """Retrieves training data from GCS.

    Args:
        bucket_name (str): Name of the GCS bucket.
        train_data_gcs_folder (str): GCS folder containing train data.

    Returns:
        List: Tuple of texts and dict of entities to be used for training.
    """
    train_data_local_folder = Path(__file__).parent.parent.resolve() / "train_data"
    download_from_gcs(bucket_name, train_data_gcs_folder, train_data_local_folder)
    train_data_files = glob.glob(os.path.join(train_data_local_folder, "*.jsonl"))
    return train_data_files


@task
def evaluate_ner(tasks: List[dict]) -> dict:
    """Computes accuracy, precision and recall of NER model out of label studio output.

    Args:
        tasks (list): List of dicts outputs of label studio annotation with following format
                        [
                            {
                            "result": [
                                {
                                    "value": {"start": 10, "end": 17, "text": "Chennai", "labels": ["LOC"]},
                                    "from_name": "label",
                                    "to_name": "text",
                                    "type": "labels",
                                    "origin": "manual",
                                }
                            ],
                            "predictions": [
                                    {
                                    "result": {"start": 10, "end": 17, "text": "Chennai", "labels": ["LOC"]},
                                    "model_version": "dummy",
                                    }
                                ],
                            }
                        ]

    Returns:
        dict: mapping {model_name: accuracy}

    """
    model_acc = dict()
    model_hits = defaultdict(int)
    for ls_task in json.loads(tasks):
        annotation_result = ls_task["result"][0]["value"]
        for key in annotation_result:
            if key == "id":
                annotation_result.pop("id")
        for prediction in ls_task["predictions"]:
            model_version = prediction["model_version"]
            model_hits[model_version] += int(prediction["result"] == annotation_result)

    num_task = len(tasks)
    for model_name, num_hits in model_hits.items():
        acc = num_hits / num_task
        model_acc[model_name] = acc
        print(f"Accuracy for {model_name}: {acc:.2f}%")
    return model_acc


@task
def load_tasks(bucket_name: str, source_blob_name: str) -> bytes:
    """Loads Label Studio annotations.

    Args:
        bucket_name (str): GCS bucket name where tasks are stored.
        source_blob_name (str): GCS blob name where tasks are stored.

    Returns:
        str: json dumped tasks
    """
    tasks = download_bytes_from_gcs(
        bucket_name=bucket_name,
        source_blob_name=source_blob_name)
    return tasks


@task
def format_tasks_for_train(tasks: bytes) -> str:
    """Format Label Studio output to be trained in spacy custom model.

    Args:
        tasks (str): json dumped tasks

    Returns:
        str: json dumped train data formatted
    """
    train_data = []
    for ls_task in json.loads(tasks):
        entities = [
            (ent["value"]["start"], ent["value"]["end"], label)
            for ent in ls_task["result"]
            for label in ent["value"]["labels"]
        ]
        if entities != []:
            train_data.append((ls_task["task"]["data"]["text"], {"entities": entities}))
    return json.dumps(train_data)


@task
def load_model(
    lang: str,
    from_gcs: bool,
    gcs_bucket: str,
    gcs_source_blob_name: str,
) -> bytes:
    """Loads spacy model either from gcs if specified or given the source language.

    Args:
        lang (str): Language in which tweets must be written(iso-code).
        from_gcs (bool): True if needs to download custom spacy model from gcs.
        gcs_bucket (str): bucket name where to retrieve spacy model if from_gcs.
        gcs_source_blob_name (str, optional): blob name where to retrieve spacy model if from_gcs.

    Returns:
        Language: spacy model
    """
    if from_gcs:
        Path("tmp").mkdir(parents=True, exist_ok=True)
        output_filename = download_from_gcs(
            gcs_bucket, gcs_source_blob_name, "tmp", explicit_filepath=True
        )[0]
        nlp = spacy.load(output_filename)
    else:
        model_name = SPACY_MODEL[lang]
    nlp = spacy.load(model_name)
    return nlp

@task
def train_model(
    train_data: str, nlp: Language, training_iterations: int = 30
) -> Language:
    """ Uses new labelled data to improve spacy NER model.

    Args:
        train_data_files (List[str]): List of data filepath to train model on. After being loaded, format \
            should be the following:
                train_data = [
                    ("Text to detect Entities in.", {"entities": [(15, 23, "PRODUCT")]}),
                    ("Flyte is another example of organisation.", {"entities": [(0, 6, "ORG")]}),
                ]
        nlp (Language): Spacy base model to train on.
        training_iterations (int): Number of training iterations to make. Defaults to 30.

    Returns:
        Language: Trained spacy model
    """
    train_data = json.loads(train_data)
    ner = nlp.get_pipe("ner")
    for _, annotations in train_data:
        for ent in annotations.get("entities"):
            ner.add_label(ent[2])
    pipe_exceptions = ["ner", "trf_wordpiecer", "trf_tok2vec"]
    unaffected_pipes = [pipe for pipe in nlp.pipe_names if pipe not in pipe_exceptions]
    with nlp.disable_pipes(*unaffected_pipes):
        optimizer = spacy.blank("en").initialize()
        for iteration in range(training_iterations):
            random.shuffle(train_data)
            losses = {}
            batches = minibatch(train_data, size=compounding(4.0, 32.0, 1.001))
            for batch in batches:
                for text, annotations in batch:
                    doc = nlp.make_doc(text)
                    example = Example.from_dict(doc, annotations)
                    nlp.update([example], drop=0.35, losses=losses, sgd=optimizer)
                    print("Iteration n°", iteration)
                    print("Losses", losses)
    upload_to_gcs("wcgl_data", "spacy_model/models/dummy.pkl", pickle.dumps(nlp))
    return nlp


@dynamic(
    cache=False,
    requests=request_resources,
    limits=limit_resources,
)
def train_model_if_necessary(tasks: bytes):#, metrics_dict: dict, model_name: str):
    metrics_dict = {"dummy": 0.5}
    model_name = "dummy"
    if metrics_dict[model_name] >= THRESHOLD_ACCURACY:
        return
    else:
        train_data = format_tasks_for_train(tasks=tasks)
        nlp = load_model(lang="en", from_gcs=False, gcs_bucket="", gcs_source_blob_name="")
        nlp = train_model(train_data=train_data, nlp=nlp, training_iterations=30)


@workflow
def main():
    config = load_config("train")
    tasks = load_tasks(bucket_name=config["bucket_label_out_name"], source_blob_name=config["label_studio_output_blob_name"])
    #metrics_dict = evaluate_ner(tasks=tasks)
    nlp = train_model_if_necessary(tasks=tasks)#, metrics_dict=metrics_dict, model_name=model_name)
    return nlp


if __name__ == "__main__":
    print(f"Trained model: {main()}")
