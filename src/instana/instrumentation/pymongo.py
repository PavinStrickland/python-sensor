# (c) Copyright IBM Corp. 2021
# (c) Copyright Instana Inc. 2020


from ..log import logger
from ..util.traceutils import get_tracer_tuple, tracing_is_off

try:
    import pymongo  # noqa: F401
    from bson import json_util
    from pymongo import monitoring

    class MongoCommandTracer(monitoring.CommandListener):
        def __init__(self):
            self.__active_commands = {}

        def started(self, event):
            tracer, parent_span, _ = get_tracer_tuple()
            # return early if we're not tracing
            if tracing_is_off():
                return

            with tracer.start_active_span("mongo", child_of=parent_span) as scope:
                self._collect_connection_tags(scope.span, event)
                self._collect_command_tags(scope.span, event)

                # include collection name into the namespace if provided
                if event.command_name in event.command:
                    scope.span.set_tag(
                        "collection", event.command.get(event.command_name)
                    )

                self.__active_commands[event.request_id] = scope

        def succeeded(self, event):
            active_span = self.__active_commands.pop(event.request_id, None)

            # return early if we're not tracing
            if active_span is None:
                return

        def failed(self, event):
            active_span = self.__active_commands.pop(event.request_id, None)

            # return early if we're not tracing
            if active_span is None:
                return

            active_span.log_exception(event.failure)

        def _collect_connection_tags(self, span, event):
            (host, port) = event.connection_id

            span.set_tag("host", host)
            span.set_tag("port", str(port))
            span.set_tag("db", event.database_name)

        def _collect_command_tags(self, span, event):
            """
            Extract MongoDB command name and arguments and attach it to the span
            """
            cmd = event.command_name
            span.set_tag("command", cmd)

            for key in ["filter", "query"]:
                if key in event.command:
                    span.set_tag("filter", json_util.dumps(event.command.get(key)))
                    break

            # The location of command documents within the command object depends on the name
            # of this command. This is the name -> command object key mapping
            cmd_doc_locations = {
                "insert": "documents",
                "update": "updates",
                "delete": "deletes",
                "aggregate": "pipeline",
            }

            cmd_doc = None
            if cmd in cmd_doc_locations:
                cmd_doc = event.command.get(cmd_doc_locations[cmd])
            elif (
                cmd.lower() == "mapreduce"
            ):  # mapreduce command was renamed to mapReduce in pymongo 3.9.0
                # mapreduce command consists of two mandatory parts: map and reduce
                cmd_doc = {
                    "map": event.command.get("map"),
                    "reduce": event.command.get("reduce"),
                }

            if cmd_doc is not None:
                span.set_tag("json", json_util.dumps(cmd_doc))

    monitoring.register(MongoCommandTracer())

    logger.debug("Instrumenting pymongo")

except ImportError:
    pass
