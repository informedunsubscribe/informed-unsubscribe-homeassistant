"""One run of the pipeline as its own short-lived process: `python3 -m app.run`.

Prints the errors from the run as a JSON list on standard output, for the app to show.
"""

import json
import logging
import sys

from . import config, db, pipeline

logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
cfg = config.load()
db.init(cfg.data_dir)
print(json.dumps(pipeline.run_once(cfg)))
