from restalchemy.api import middlewares as ra_middlewares
from restalchemy.common import contexts as common_contexts
from restalchemy.common import exceptions as comm_exc

from workspace.common.clients import zulip as zulip_client


class UserContext(common_contexts.Context):
    def __init__(self, user_id, **kwargs):
        # Initialize base context (DB engine, etc.)
        super().__init__(**kwargs)
        # Additional user_id field fetched from external service
        self._user_id = user_id

    @property
    def user_id(self):
        return self._user_id


class UserContextMiddleware(ra_middlewares.Middleware):
    EXCLUDE_PATHS = [
        "/",
        "/v1/",
        "/specifications/",
        "/specifications/3.0.3",
    ]

    def process_request(self, req):
        # If context already exists, do not override it and do not call Zulip
        if hasattr(req, "context") and req.context is not None:
            return None

        if req.path in self.EXCLUDE_PATHS:
            return None

        # Build Zulip endpoint on the same domain as the incoming request
        proto = req.headers.get("X-Forwarded-Proto", req.scheme)
        base_url = f"{proto}://{req.headers['Host']}"
        client = zulip_client.ZulipClient(endpoint=base_url, timeout=3)

        headers = {}

        auth = req.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth

        cookie = req.headers.get("Cookie")
        if cookie:
            headers["Cookie"] = cookie

        try:
            user_id = client.get_current_user_id(headers=headers)
        except Exception as exc:
            # Let bazooka HTTP exceptions bubble up so
            # ErrorsHandlerMiddleware can format them. Wrap any
            # other unexpected error into RESTAlchemy
            # ValidationErrorException so that it becomes HTTP 400.
            if hasattr(exc, "code"):
                # bazooka HTTP exceptions have .code and will be handled
                # by ErrorsHandlerMiddleware.
                raise
            raise comm_exc.ValidationErrorException()

        # Create context only if it does not exist yet
        req.context = UserContext(user_id=user_id)
        return None
