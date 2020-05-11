# -*- coding: utf-8 -*-

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
test_limit
----------------------------------
Tests for `limit` module.
"""

from unittest import mock
import uuid

from openstack import exceptions as sdk_exceptions
from openstack.identity.v3 import endpoint
from openstack.identity.v3 import service
from openstack.identity.v3 import limit as klimit
from openstack.identity.v3 import registered_limit
from oslo_config import cfg
from oslo_config import fixture as config_fixture
from oslotest import base

from oslo_limit import exception
from oslo_limit import limit
from oslo_limit import opts

CONF = cfg.CONF
TEST_ENDPOINT_ID = ""

class TestEnforcer(base.BaseTestCase):

    def setUp(self):
        super(TestEnforcer, self).setUp()
        self.deltas = dict()
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            auth_type='password'
        )
        opts.register_opts(CONF)
        self.config_fixture.config(
            group='oslo_limit',
            auth_url='http://identity.example.com'
        )

        self.get_service_and_region = mock.MagicMock()
        self.get_service_and_region.return_value = ("service_id", "region_id")
        limit._get_service_and_region = self.get_service_and_region

        limit._SDK_CONNECTION = mock.MagicMock()
        json = mock.MagicMock()
        json.json.return_value = {"model": {"name": "flat"}}
        limit._SDK_CONNECTION.get.return_value = json

    def _get_usage_for_project(self, project_id, resource_names):
        return {"a": 1}

    def test_usage_callback_must_be_callable(self):
        invalid_callback_types = [uuid.uuid4().hex, 5, 5.1]

        for invalid_callback in invalid_callback_types:
            self.assertRaises(
                ValueError,
                limit.Enforcer,
                invalid_callback
            )

    def test_deltas_must_be_a_dictionary(self):
        project_id = uuid.uuid4().hex
        invalid_delta_types = [uuid.uuid4().hex, 5, 5.1, True, [], None, {}]
        enforcer = limit.Enforcer(self._get_usage_for_project)

        for invalid_delta in invalid_delta_types:
            self.assertRaises(
                ValueError,
                enforcer.enforce,
                project_id,
                invalid_delta
            )

    def test_project_id_must_be_a_string(self):
        enforcer = limit.Enforcer(self._get_usage_for_project)
        invalid_delta_types = [{}, 5, 5.1, True, False, [], None, ""]
        for invalid_project_id in invalid_delta_types:
            self.assertRaises(
                ValueError,
                enforcer.enforce,
                invalid_project_id,
                {}
            )

    def test_set_model_impl(self):
        enforcer = limit.Enforcer(self._get_usage_for_project)
        self.assertIsInstance(enforcer.model, limit._FlatEnforcer)

    def test_get_model_impl(self):
        json = mock.MagicMock()
        limit._SDK_CONNECTION.get.return_value = json

        json.json.return_value = {"model": {"name": "flat"}}
        enforcer = limit.Enforcer(self._get_usage_for_project)
        flat_impl = enforcer._get_model_impl(self._get_usage_for_project)
        self.assertIsInstance(flat_impl, limit._FlatEnforcer)

        json.json.return_value = {"model": {"name": "strict-two-level"}}
        flat_impl = enforcer._get_model_impl(self._get_usage_for_project)
        self.assertIsInstance(flat_impl, limit._StrictTwoLevelEnforcer)

        json.json.return_value = {"model": {"name": "foo"}}
        e = self.assertRaises(ValueError, enforcer._get_model_impl,
                              self._get_usage_for_project)
        self.assertEqual("enforcement model foo is not supported", str(e))

    @mock.patch.object(limit._FlatEnforcer, "enforce")
    def test_enforce(self, mock_enforce):
        enforcer = limit.Enforcer(self._get_usage_for_project)
        project_id = uuid.uuid4().hex
        deltas = {"a": 1}

        enforcer.enforce(project_id, deltas)

        mock_enforce.assert_called_once_with(project_id, deltas)


class TestFlatEnforcer(base.BaseTestCase):
    def setUp(self):
        super(TestFlatEnforcer, self).setUp()
        self.mock_conn = mock.MagicMock()
        self.get_service_and_region = mock.MagicMock()
        self.get_service_and_region.return_value = ("service_id", "region_id")
        limit._get_service_and_region = self.get_service_and_region
        limit._SDK_CONNECTION = self.mock_conn

    @mock.patch.object(limit._EnforcerUtils, "get_project_limits")
    def test_enforce(self, mock_get_limits):
        mock_usage = mock.MagicMock()

        project_id = uuid.uuid4().hex
        deltas = {"a": 1, "b": 1}
        mock_get_limits.return_value = [("a", 1), ("b", 2)]
        mock_usage.return_value = {"a": 0, "b": 1}

        enforcer = limit._FlatEnforcer(mock_usage)
        enforcer.enforce(project_id, deltas)

        self.get_service_and_region.assert_called_once()
        mock_get_limits.assert_called_once_with(project_id, ["a", "b"])
        mock_usage.assert_called_once_with(project_id, ["a", "b"])

    @mock.patch.object(limit._EnforcerUtils, "get_project_limits")
    def test_enforce_raises_on_over(self, mock_get_limits):
        mock_usage = mock.MagicMock()

        project_id = uuid.uuid4().hex
        deltas = {"a": 2, "b": 1}
        mock_get_limits.return_value = [("a", 1), ("b", 2)]
        mock_usage.return_value = {"a": 0, "b": 1}

        enforcer = limit._FlatEnforcer(mock_usage)
        e = self.assertRaises(exception.ProjectOverLimit, enforcer.enforce,
                              project_id, deltas)
        expected = ("Project %s is over a limit for "
                    "[Resource a is over limit of 1 due to current usage 0 "
                    "and delta 2]")
        self.assertEqual(expected % project_id, str(e))
        self.assertEqual(project_id, e.project_id)
        self.assertEqual(1, len(e.over_limit_info_list))
        over_a = e.over_limit_info_list[0]
        self.assertEqual("a", over_a.resource_name)
        self.assertEqual(1, over_a.limit)
        self.assertEqual(0, over_a.current_usage)
        self.assertEqual(2, over_a.delta)

    @mock.patch.object(limit._EnforcerUtils, "get_project_limits")
    def test_enforce_raises_on_missing_limit(self, mock_get_limits):
        mock_usage = mock.MagicMock()

        project_id = uuid.uuid4().hex
        deltas = {"a": 0, "b": 0}
        mock_get_limits.side_effect = exception.ProjectOverLimit(
            project_id, [exception.OverLimitInfo("a", 0, 0, 0)])

        enforcer = limit._FlatEnforcer(mock_usage)
        self.assertRaises(exception.ProjectOverLimit, enforcer.enforce,
                          project_id, deltas)


class TestEnforcerUtils(base.BaseTestCase):
    def setUp(self):
        super(TestEnforcerUtils, self).setUp()
        self.mock_conn = mock.MagicMock()
        self.get_service_and_region = mock.MagicMock()
        self.get_service_and_region.return_value = ("service_id", "region_id")
        limit._get_service_and_region = self.get_service_and_region
        limit._SDK_CONNECTION = self.mock_conn

    def test_get_registered_limit_empty(self):
        self.mock_conn.registered_limits.return_value = iter([])

        utils = limit._EnforcerUtils()
        reg_limit = utils._get_registered_limit("foo")

        self.assertIsNone(reg_limit)

    def test_get_registered_limit(self):
        foo = registered_limit.RegisteredLimit()
        foo.resource_name = "foo"
        self.mock_conn.registered_limits.return_value = iter([foo])

        utils = limit._EnforcerUtils()
        reg_limit = utils._get_registered_limit("foo")

        self.assertEqual(foo, reg_limit)

    def test_get_project_limits(self):
        project_id = uuid.uuid4().hex

        # a is a project limit, b, c and d don't have one
        empty_iterator = iter([])
        a = klimit.Limit()
        a.resource_name = "a"
        a.resource_limit = 1
        a_iterator = iter([a])
        self.mock_conn.limits.side_effect = [a_iterator, empty_iterator,
                                             empty_iterator, empty_iterator]

        # b has a limit, but c and d doesn't, a isn't ever checked
        b = registered_limit.RegisteredLimit()
        b.resource_name = "b"
        b.default_limit = 2
        b_iterator = iter([b])
        self.mock_conn.registered_limits.side_effect = [b_iterator,
                                                        empty_iterator,
                                                        empty_iterator]

        utils = limit._EnforcerUtils()
        limits = utils.get_project_limits(project_id, ["a", "b"])
        self.assertEqual([('a', 1), ('b', 2)], limits)

        e = self.assertRaises(exception.ProjectOverLimit,
                              utils.get_project_limits,
                              project_id, ["c", "d"])
        expected = ("Project %s is over a limit for "
                    "[Resource c is over limit of 0 due to current usage 0 "
                    "and delta 0, "
                    "Resource d is over limit of 0 due to current usage 0 "
                    "and delta 0]")
        self.assertEqual(expected % project_id, str(e))
        self.assertEqual(project_id, e.project_id)
        self.assertEqual(2, len(e.over_limit_info_list))
        over_c = e.over_limit_info_list[0]
        self.assertEqual("c", over_c.resource_name)
        over_d = e.over_limit_info_list[1]
        self.assertEqual("d", over_d.resource_name)
        self.assertEqual(0, over_d.limit)
        self.assertEqual(0, over_d.current_usage)
        self.assertEqual(0, over_d.delta)


class TestGetServiceAndRegion(base.BaseTestCase):

    def setUp(self):
        super(TestGetServiceAndRegion, self).setUp()
        self.mock_conn = mock.MagicMock()
        limit._SDK_CONNECTION = self.mock_conn

    def test_without_config_raise_value_error(self):
        self.assertRaises(ValueError, limit._get_service_and_region)

    def test_wrong_endpoint_id_raise_value_error(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            endpoint_id=uuid.uuid4().hex
        )

        self.mock_conn.get_endpoint.side_effect = \
            sdk_exceptions.ResourceNotFound()

        self.assertRaises(ValueError, limit._get_service_and_region)

    def test_endpoint_id(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            endpoint_id=uuid.uuid4().hex
        )

        fake_endpoint = endpoint.Endpoint(
            service_id="service_id",
            region_id="region_id"
        )
        self.mock_conn.get_endpoint.return_value = fake_endpoint

        service_id, region_id = limit._get_service_and_region()

        self.mock_conn.get_endpoint.assert_called_once_with(
            CONF.oslo_limit.endpoint_id
        )

        self.assertEqual("service_id", service_id)
        self.assertEqual("region_id", region_id)

    def test_endpoint_id_override_service_name(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            endpoint_id=uuid.uuid4().hex,
            service_name="my_service",
            region_name="my_region"
        )

        fake_endpoint = endpoint.Endpoint(
            service_id="service_id",
            region_id="region_id"
        )
        self.mock_conn.get_endpoint.return_value = fake_endpoint

        service_id, region_id = limit._get_service_and_region()

        self.mock_conn.get_endpoint.assert_called_once_with(
            CONF.oslo_limit.endpoint_id
        )

        self.assertEqual("service_id", service_id)
        self.assertEqual("region_id", region_id)

    def test_service_name(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            service_name="my_service",
            region_name="my_region"
        )

        self.mock_conn.services.return_value = [
            service.Service(id="service_id")
        ]

        service_id, region_id = limit._get_service_and_region()

        self.mock_conn.get_endpoint.assert_not_called()
        self.mock_conn.services.assert_called_once_with(name="my_service")

        self.assertEqual("service_id", service_id)
        self.assertEqual("my_region", region_id)

    def test_wrong_service_name(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            service_name="my_wrong_service",
            region_name="my_region"
        )

        self.mock_conn.services.return_value = []

        self.assertRaises(ValueError, limit._get_service_and_region)

        self.mock_conn.get_endpoint.assert_not_called()
        self.mock_conn.services.assert_called_once_with(
            name="my_wrong_service"
        )

    def test_service_name_multiple_result_raise_value_error(self):
        self.config_fixture = self.useFixture(config_fixture.Config(CONF))
        self.config_fixture.config(
            group='oslo_limit',
            service_name="my_service",
            region_name="my_region"
        )

        self.mock_conn.services.return_value = [
            service.Service(id="service_1"),
            service.Service(id="service_2"),
        ]

        self.assertRaises(ValueError, limit._get_service_and_region)

        self.mock_conn.get_endpoint.assert_not_called()
        self.mock_conn.services.assert_called_once_with(
            name="my_service"
        )
