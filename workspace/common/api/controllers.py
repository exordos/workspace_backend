#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Pagination controller with composite primary key support.

This is a copy of ``restalchemy.api.controllers.BaseResourceControllerPaginated``
reworked so a controller can declare a complex (composite) primary key::

    class MyController(BaseResourceControllerComplexPaginated):
        __complex_primary_key__ = ["uuid", "user_uuid"]

The first column of ``__complex_primary_key__`` is the *public* identifier:
it is the one exposed in the resource URL and used as the pagination marker
(it must be globally unique, e.g. a UUID).  The remaining columns are *scoped*
parts of the key (e.g. ``user_uuid``) which are mixed in automatically from the
request context via :meth:`_get_complex_pk_scope` and never have to be passed by
the API caller.  The whole composite key is used as the deterministic ordering
for cursor based pagination.
"""

from restalchemy.api import controllers as ra_controllers
from restalchemy.common import exceptions as exc
from restalchemy.dm import filters as dm_filters


class ComplexPaginationFilterBuilder:
    """Pagination cursor builder that does not rely on the model's single
    ``id_property``.

    Behaves exactly like
    ``restalchemy.api.controllers.PaginationFilterBuilder`` but the identifier
    column is supplied explicitly, so it works for models whose primary key is
    composite.
    """

    def __init__(
        self,
        model,
        marker_id,
        id_name,
        sort_column=None,
        sort_direction="asc",
    ):
        self.marker_id = marker_id
        self.sort_col = sort_column
        self.sort_dir = sort_direction
        self.id_name = id_name
        self.sort_value = self._fetch_sort_val(model)

    def _fetch_sort_val(self, model):
        """Fetch the sort value for this marker ID."""
        # No need for an extra query if we sort by the public id itself.
        if self.sort_col in (None, self.id_name):
            return self.marker_id

        marker_row = model.objects.get_one(
            filters={self.id_name: dm_filters.EQ(self.marker_id)}
        )
        return getattr(marker_row, self.sort_col)

    def build_filter(self):
        """Build the compound pagination filter."""
        if not self.sort_col:
            return {self.id_name: dm_filters.GT(self.marker_id)}

        id_op = dm_filters.GT if self.sort_dir == "asc" else dm_filters.LT
        if self.sort_col == self.id_name:
            return {self.id_name: id_op(self.marker_id)}

        return dm_filters.OR(
            {self.sort_col: id_op(self.sort_value)},
            dm_filters.AND(
                {self.sort_col: dm_filters.EQ(self.sort_value)},
                {self.id_name: dm_filters.GT(self.marker_id)},
            ),
        )


class ComplexPrimaryKeyPaginationMixin(object):
    """Marker based pagination mixin aware of a composite primary key.

    See module docstring for ``__complex_primary_key__`` semantics.
    """

    __complex_primary_key__ = ["uuid"]

    _pagination_limit = 0
    _header_page_limit = "X-Pagination-Limit"
    _header_page_marker = "X-Pagination-Marker"

    _param_page_limit = "page_limit"
    _param_page_marker = "page_marker"

    # -- complex primary key helpers -------------------------------------

    @classmethod
    def _complex_pk(cls):
        return list(cls.__complex_primary_key__)

    @classmethod
    def _public_pk_name(cls):
        """Name of the publicly addressable (and unique) key column."""
        return cls._complex_pk()[0]

    @classmethod
    def _scoped_pk_names(cls):
        """Key columns that are mixed in automatically from the context."""
        return cls._complex_pk()[1:]

    def _get_complex_pk_scope(self):
        """Return ``{scoped_pk_name: value}`` taken from the request context.

        Must be implemented by the concrete controller for every column listed
        in ``__complex_primary_key__`` except the public (first) one.
        """
        if not self._scoped_pk_names():
            return {}
        raise NotImplementedError(
            "%s must implement _get_complex_pk_scope() to resolve %r"
            % (type(self).__name__, self._scoped_pk_names())
        )

    def _scoped_pk_filters(self):
        scope = self._get_complex_pk_scope()
        return {
            name: dm_filters.EQ(scope[name]) for name in self._scoped_pk_names()
        }

    # -- pagination ------------------------------------------------------

    def _create_response(self, body, status, headers):
        if self._pagination_limit:
            headers[self._header_page_limit] = str(self._pagination_limit)
            if len(body) == self._pagination_limit:
                headers[self._header_page_marker] = str(
                    getattr(body[-1], self._public_pk_name())
                )

        return super(ComplexPrimaryKeyPaginationMixin, self)._create_response(
            body, status, headers
        )

    def _prepare_pagination_meta(self):
        try:
            self._pagination_limit = int(
                self._req.api_context.params.get(self._param_page_limit, 0)
            )
            if self._pagination_limit < 0:
                raise ValueError()
        except ValueError:
            raise exc.ParseError(value="%s" % (self._pagination_limit,))

        self._pagination_marker = self._req.api_context.params.get(
            self._param_page_marker
        )
        if self._pagination_marker:
            self._pagination_marker = (
                self._parse_resource_uuid(
                    self._public_pk_name(),
                    self._pagination_marker,
                    self.get_resource().get_id_type(),
                )
                if self.__resource__
                else self._pagination_marker
            )

    def do_collection(self, parent_resource=None):
        self._prepare_pagination_meta()

        return super(
            ComplexPrimaryKeyPaginationMixin, self
        ).do_collection(parent_resource=parent_resource)

    def _process_storage_filters(self, filters, order_by=None):
        self._validate_params(filters, order_by)
        filters, order_by = self._build_pagination_with_cursor(filters, order_by)
        return self.model.objects.get_all(
            filters=filters,
            limit=self._pagination_limit,
            order_by=order_by,
        )

    def _validate_params(self, filters, order_by):
        if order_by and len(order_by) > 1:
            raise exc.ValidationSortNumberError()

    def _build_pagination_with_cursor(self, filters, order_by):
        id_name = self._public_pk_name()
        if self._pagination_marker:
            sort_col, sort_dir = (
                next(iter(order_by.items())) if order_by else (None, "asc")
            )
            cursor = ComplexPaginationFilterBuilder(
                self.model,
                self._pagination_marker,
                id_name,
                sort_col,
                sort_dir,
            )
            pagination_filters = cursor.build_filter()
            filters = dm_filters.AND(pagination_filters, filters)

        # Build final ordering: keep the optional user sort, then append every
        # primary key column as a deterministic tiebreaker so no row is skipped
        # or duplicated between pages.
        order_by = order_by.copy() if order_by else {}
        for pk_name in self._complex_pk():
            if pk_name not in order_by:
                order_by[pk_name] = "asc"

        return filters, order_by

    def paginated_filter(self, filters, order_by=None):
        custom_filters, storage_filters = self._split_filters(filters)

        cleaned_results = []

        while len(cleaned_results) < self._pagination_limit:
            result = self._process_storage_filters(
                storage_filters, order_by=order_by
            )

            if not len(result):
                break

            self._pagination_marker = getattr(result[-1], self._public_pk_name())

            cleaned_results.extend(
                self._process_custom_filters(result, custom_filters)
            )

        if len(cleaned_results) > self._pagination_limit:
            cleaned_results = cleaned_results[: self._pagination_limit]

        return cleaned_results


class BaseResourceControllerComplexPaginated(
    ComplexPrimaryKeyPaginationMixin,
    ra_controllers.BaseResourceController,
):
    """Paginated resource controller for models with a composite primary key.

    The scoped parts of the key (everything after the public column) are mixed
    in automatically from the request context for create/get/update/delete and
    filter, so callers only ever deal with the public ``uuid``.
    """

    def create(self, **kwargs):
        kwargs.update(self._get_complex_pk_scope())
        return super(BaseResourceControllerComplexPaginated, self).create(
            **kwargs
        )

    def get(self, uuid, **kwargs):
        filters = {self._public_pk_name(): dm_filters.EQ(uuid)}
        filters.update(self._scoped_pk_filters())
        filters.update(kwargs)
        return self.model.objects.get_one(filters=filters)

    def delete(self, uuid):
        self.get(uuid=uuid).delete()

    def update(self, uuid, **kwargs):
        dm = self.get(uuid=uuid)
        dm.update_dm(values=kwargs)
        dm.update()
        return dm

    def filter(self, filters, order_by=None):
        filters = (filters or {}).copy()
        filters.update(self._scoped_pk_filters())

        if self._pagination_limit:
            return self.paginated_filter(filters, order_by=order_by)

        return super(
            ComplexPrimaryKeyPaginationMixin, self
        ).filter(filters, order_by=order_by)
