import logging
import signal
import sys

from . import config, db, pipeline, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# The app is the container's first process, which ignores a stop request unless it handles it;
# without this Home Assistant waits, kills the app and reports an error.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

cfg = config.load()
db.init(cfg.data_dir)
worker = pipeline.Worker(cfg, run=pipeline.run_in_helper)
server = web.make_server(cfg, worker)
worker.after_run = lambda: web.announce(server)
worker.start()
server.serve_forever()
