# (c) Copyright IBM Corp. 2021
# (c) Copyright Instana Inc. 2020


import inspect
import json

import wrapt

from ..log import logger
from ..singletons import agent, tracer
from ..util.traceutils import get_tracer_tuple, tracing_is_off

try:
    import boto3  # noqa: F401
    import opentracing as ot  # type: ignore
    from boto3.s3 import inject  # noqa: F401

    def extract_custom_headers(span, headers):
        if agent.options.extra_http_headers is None or headers is None:
            return
        try:
            for custom_header in agent.options.extra_http_headers:
                if custom_header in headers:
                    span.set_tag(
                        "http.header.%s" % custom_header, headers[custom_header]
                    )

        except Exception:
            logger.debug("extract_custom_headers: ", exc_info=True)

    def lambda_inject_context(payload, scope):
        """
        When boto3 lambda client 'Invoke' is called, we want to inject the tracing context.
        boto3/botocore has specific requirements:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/lambda.html#Lambda.Client.invoke
        """
        try:
            invoke_payload = payload.get("Payload", {})

            if not isinstance(invoke_payload, dict):
                invoke_payload = json.loads(invoke_payload)

            tracer.inject(scope.span.context, ot.Format.HTTP_HEADERS, invoke_payload)
            payload["Payload"] = json.dumps(invoke_payload)
        except Exception:
            logger.debug("non-fatal lambda_inject_context: ", exc_info=True)

    @wrapt.patch_function_wrapper("botocore.auth", "SigV4Auth.add_auth")
    def emit_add_auth_with_instana(wrapped, instance, args, kwargs):
        if not tracing_is_off() and tracer.active_span:
            extract_custom_headers(tracer.active_span, args[0].headers)
        return wrapped(*args, **kwargs)

    @wrapt.patch_function_wrapper("botocore.client", "BaseClient._make_api_call")
    def make_api_call_with_instana(wrapped, instance, arg_list, kwargs):
        # If we're not tracing, just return
        if tracing_is_off():
            return wrapped(*arg_list, **kwargs)

        tracer, parent_span, _ = get_tracer_tuple()

        with tracer.start_active_span("boto3", child_of=parent_span) as scope:
            try:
                operation = arg_list[0]
                payload = arg_list[1]

                scope.span.set_tag("op", operation)
                scope.span.set_tag("ep", instance._endpoint.host)
                scope.span.set_tag("reg", instance._client_config.region_name)

                scope.span.set_tag(
                    "http.url", instance._endpoint.host + ":443/" + arg_list[0]
                )
                scope.span.set_tag("http.method", "POST")

                # Don't collect payload for SecretsManager
                if not hasattr(instance, "get_secret_value"):
                    scope.span.set_tag("payload", payload)

                # Inject context when invoking lambdas
                if "lambda" in instance._endpoint.host and operation == "Invoke":
                    lambda_inject_context(payload, scope)

            except Exception:
                logger.debug("make_api_call_with_instana: collect error", exc_info=True)

            try:
                result = wrapped(*arg_list, **kwargs)

                if isinstance(result, dict):
                    http_dict = result.get("ResponseMetadata")
                    if isinstance(http_dict, dict):
                        status = http_dict.get("HTTPStatusCode")
                        if status is not None:
                            scope.span.set_tag("http.status_code", status)
                        headers = http_dict.get("HTTPHeaders")
                        extract_custom_headers(scope.span, headers)

                return result
            except Exception as exc:
                scope.span.mark_as_errored({"error": exc})
                raise

    def s3_inject_method_with_instana(wrapped, instance, arg_list, kwargs):
        # If we're not tracing, just return
        if tracing_is_off():
            return wrapped(*arg_list, **kwargs)

        fas = inspect.getfullargspec(wrapped)
        fas_args = fas.args
        fas_args.remove("self")

        tracer, parent_span, _ = get_tracer_tuple()

        with tracer.start_active_span("boto3", child_of=parent_span) as scope:
            try:
                operation = wrapped.__name__
                scope.span.set_tag("op", operation)
                scope.span.set_tag("ep", instance._endpoint.host)
                scope.span.set_tag("reg", instance._client_config.region_name)

                scope.span.set_tag(
                    "http.url", instance._endpoint.host + ":443/" + operation
                )
                scope.span.set_tag("http.method", "POST")

                arg_length = len(arg_list)
                if arg_length > 0:
                    payload = {}
                    for index in range(arg_length):
                        if fas_args[index] in ["Filename", "Bucket", "Key"]:
                            payload[fas_args[index]] = arg_list[index]
                    scope.span.set_tag("payload", payload)
            except Exception:
                logger.debug(
                    "s3_inject_method_with_instana: collect error", exc_info=True
                )

            try:
                return wrapped(*arg_list, **kwargs)
            except Exception as exc:
                scope.span.mark_as_errored({"error": exc})
                raise

    for method in [
        "upload_file",
        "upload_fileobj",
        "download_file",
        "download_fileobj",
    ]:
        wrapt.wrap_function_wrapper(
            "boto3.s3.inject", method, s3_inject_method_with_instana
        )

    logger.debug("Instrumenting boto3")
except ImportError:
    pass
