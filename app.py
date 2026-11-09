"""LocaTool Flask entry point. Local development includes one worker."""
import os
import logging
from threading import Thread

from translation_app import create_app

app = create_app()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # The built-in server runs in one process; production WSGI deployments use
    # independently managed workers and do not execute this block.
    from translation_app.jobs import run_worker
    Thread(target=run_worker, args=(app.extensions['jobs'],), daemon=True,
           name='locatool-local-worker').start()
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '5000')), debug=False)
