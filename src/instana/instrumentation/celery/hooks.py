# (c) Copyright IBM Corp. 2021
# (c) Copyright Instana Inc. 2020


import opentracing  # type: ignore

from ...log import logger
from ...singletons import tracer
from ...util.traceutils import get_active_tracer

try:
    from urllib import parse

    from celery import registry, signals

    from .catalog import (
        get_task_id,
        task_catalog_get,
        task_catalog_pop,
        task_catalog_push,
    )

    def add_broker_tags(span, broker_url):
        try:
            url = parse.urlparse(broker_url)

            # Add safety for edge case where scheme may not be a string
            url_scheme = str(url.scheme)
            span.set_tag("scheme", url_scheme)

            if url.hostname is None:
                span.set_tag("host", "localhost")
            else:
                span.set_tag("host", url.hostname)

            if url.port is None:
                # Set default port if not specified
                if url_scheme == "redis":
                    span.set_tag("port", "6379")
                elif "amqp" in url_scheme:
                    span.set_tag("port", "5672")
                elif "sqs" in url_scheme:
                    span.set_tag("port", "443")
            else:
                span.set_tag("port", str(url.port))
        except Exception:
            logger.debug("Error parsing broker URL: %s" % broker_url, exc_info=True)

    @signals.task_prerun.connect
    def task_prerun(*args, **kwargs):
        try:
            ctx = None
            task = kwargs.get("sender", None)
            task_id = kwargs.get("task_id", None)
            task = registry.tasks.get(task.name)

            headers = task.request.get("headers", {})
            if headers is not None:
                ctx = tracer.extract(
                    opentracing.Format.HTTP_HEADERS,
                    headers,
                    disable_w3c_trace_context=True,
                )

            scope = tracer.start_active_span("celery-worker", child_of=ctx)
            scope.span.set_tag("task", task.name)
            scope.span.set_tag("task_id", task_id)
            add_broker_tags(scope.span, task.app.conf["broker_url"])

            # Store the scope on the task to eventually close it out on the "after" signal
            task_catalog_push(task, task_id, scope, True)
        except:
            logger.debug("task_prerun: ", exc_info=True)

    @signals.task_postrun.connect
    def task_postrun(*args, **kwargs):
        try:
            task = kwargs.get("sender", None)
            task_id = kwargs.get("task_id", None)
            scope = task_catalog_pop(task, task_id, True)
            if scope is not None:
                scope.close()
        except:
            logger.debug("after_task_publish: ", exc_info=True)

    @signals.task_failure.connect
    def task_failure(*args, **kwargs):
        try:
            task_id = kwargs.get("task_id", None)
            task = kwargs["sender"]
            scope = task_catalog_get(task, task_id, True)

            if scope is not None:
                scope.span.set_tag("success", False)
                exc = kwargs.get("exception", None)
                if exc is None:
                    scope.span.mark_as_errored()
                else:
                    scope.span.log_exception(kwargs["exception"])
        except:
            logger.debug("task_failure: ", exc_info=True)

    @signals.task_retry.connect
    def task_retry(*args, **kwargs):
        try:
            task_id = kwargs.get("task_id", None)
            task = kwargs["sender"]
            scope = task_catalog_get(task, task_id, True)

            if scope is not None:
                reason = kwargs.get("reason", None)
                if reason is not None:
                    scope.span.set_tag("retry-reason", reason)
        except:
            logger.debug("task_failure: ", exc_info=True)

    @signals.before_task_publish.connect
    def before_task_publish(*args, **kwargs):
        try:
            active_tracer = get_active_tracer()
            if active_tracer is not None:
                body = kwargs["body"]
                headers = kwargs["headers"]
                task_name = kwargs["sender"]
                task = registry.tasks.get(task_name)
                task_id = get_task_id(headers, body)

                scope = active_tracer.start_active_span(
                    "celery-client", child_of=active_tracer.active_span
                )
                scope.span.set_tag("task", task_name)
                scope.span.set_tag("task_id", task_id)
                add_broker_tags(scope.span, task.app.conf["broker_url"])

                # Context propagation
                context_headers = {}
                active_tracer.inject(
                    scope.span.context,
                    opentracing.Format.HTTP_HEADERS,
                    context_headers,
                    disable_w3c_trace_context=True,
                )

                # Fix for broken header propagation
                # https://github.com/celery/celery/issues/4875
                task_headers = kwargs.get("headers") or {}
                task_headers.setdefault("headers", {})
                task_headers["headers"].update(context_headers)
                kwargs["headers"] = task_headers

                # Store the scope on the task to eventually close it out on the "after" signal
                task_catalog_push(task, task_id, scope, False)
        except:
            logger.debug("before_task_publish: ", exc_info=True)

    @signals.after_task_publish.connect
    def after_task_publish(*args, **kwargs):
        try:
            task_id = get_task_id(kwargs["headers"], kwargs["body"])
            task = registry.tasks.get(kwargs["sender"])
            scope = task_catalog_pop(task, task_id, False)
            if scope is not None:
                scope.close()
        except:
            logger.debug("after_task_publish: ", exc_info=True)

    logger.debug("Instrumenting celery")
except ImportError:
    pass
