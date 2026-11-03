"""Django-side probe worker; depends only on the selected release's packages."""

import json
import os
import sys
from contextlib import redirect_stdout
from importlib import import_module


def probe_model(model_label: str, row_limit: int) -> dict[str, object]:
    """Read bounded rows and every concrete field using an initialized Django app.

    Return an Outcome-shaped mapping with scenario_id='orm:<label>:read'. On
    success, body contains the row dictionaries. On query failure, record the
    exception type/message and whether it is a Django IntegrityError. Reject
    nonpositive row limits. Preserve the model's declared default ordering.
    """
    raise NotImplementedError("model read probe is not implemented")


def main() -> None:
    request = json.load(sys.stdin)
    sys.path.insert(0, sys.argv[1])
    os.environ["DJANGO_SETTINGS_MODULE"] = request["settings_module"]
    django = import_module("django")
    settings = import_module("django.conf").settings
    connections = import_module("django.db").connections
    if set(settings.DATABASES) != {"default"} or settings.DATABASE_ROUTERS:
        raise RuntimeError("Probe worker requires one default database without routers")
    settings.DATABASES = {"default": request["database"]}

    try:
        with redirect_stdout(sys.stderr):
            django.setup()
            if request["operation"] == "seed":
                import_module("django.core.management").call_command(
                    "loaddata", request["fixture"], database="default", verbosity=0
                )
                outcomes = []
            else:
                if request["operation"] != "read":
                    raise ValueError("unknown probe worker operation")
                apps = import_module("django.apps").apps
                labels = request["model_labels"]
                if labels is None:
                    labels = sorted(model._meta.label for model in apps.get_models())
                outcomes = [
                    probe_model(label, request["row_limit"]) for label in labels
                ]
    finally:
        connections.close_all()

    print(json.dumps(outcomes, default=str))


if __name__ == "__main__":
    main()
