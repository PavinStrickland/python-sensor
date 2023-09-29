# (c) Copyright IBM Corp. 2021
# (c) Copyright Instana Inc. 2019

from __future__ import absolute_import

try:
    import flask

    # `signals_available` indicates whether the Flask process is running with or without blinker support:
    # https://pypi.org/project/blinker/
    #
    # Blinker support is preferred but we do the best we can when it's not available.
    #
    flask_version = tuple(map(int, flask.__version__.split('.')))
    if flask_version < (2, 3, 0):
      from flask.signals import signals_available
    else:
      # Beginning from 2.3.0 as stated in the notes
      # https://flask.palletsprojects.com/en/2.3.x/changes/#version-2-3-0
      # "Signals are always available. blinker>=1.6.2 is a required dependency.
      # The signals_available attribute is deprecated. #5056"
      signals_available = True

    from . import common

    if signals_available is True:
        import instana.instrumentation.flask.with_blinker
    else:
        import instana.instrumentation.flask.vanilla
except ImportError:
    pass
