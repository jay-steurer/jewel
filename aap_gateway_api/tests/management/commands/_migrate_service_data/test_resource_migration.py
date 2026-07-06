"""Tests for ResourceMigrationMixin: migrate_conflicting_user, merge_users,
correcting_user_service_id, migrating_user_with_invalid_email,
updating_resource_data_for_invalid_resource,
process_migrate_resource_item_*, reconcile_existing_resource_*.
"""

import uuid
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from ansible_base.resource_registry.models import Resource, service_id
from ansible_base.resource_registry.rest_client import ResourceRequestBody
from django.core.management import call_command

from aap_gateway_api.management.commands.migrate_service_data import Command as MigrateCommand
from aap_gateway_api.models import MigratedUserMetadata, Organization, User
from aap_gateway_api.tests.management.commands._migrate_service_data.conftest import SEP_CHAR, assert_all_resources_synced, setup_basic_service_client_mocks


@pytest.mark.django_db(transaction=True)
def test_migrate_conflicting_user(migration_service, admin_user, admin_api_client, patched_resource_client, patched_load_rbac):
    assert not User.objects.filter(username="natasha").exists()
    assert not User.objects.filter(username="hawkeye").exists()

    from aap_gateway_api.models import ServiceCluster, ServiceType

    fake_service_type = ServiceType.objects.create(name="fake")
    fake_service_cluster = ServiceCluster.objects.create(name="fake", service_type=fake_service_type)

    u = User.objects.create(username="hawkeye")
    MigratedUserMetadata.objects.create(user=u, service=fake_service_cluster, original_username="hawkeye")

    service_client = patched_resource_client(service=migration_service, user=admin_user, raise_if_bad_request=True)

    call_command("migrate_service_data", username=admin_user.username)

    pre_sync_resources = service_client.list_resources(
        {
            "service_id": service_client.service.service_cluster.service_id,
            "content_type__resource_type__name": "shared.user",
            "not__name": admin_user.username,
        }
    ).json()

    assert len(pre_sync_resources['results']) > 0

    assert_all_resources_synced(admin_api_client, migration_service, service_client)

    assert User.objects.filter(username="natasha").exists()
    assert User.objects.filter(username="hawkeye").exists()

    assert not User.objects.filter(username=f'{service_client.service.api_slug}{SEP_CHAR}hawkeye').exists()

    hawkeye_user = User.objects.get(username="hawkeye")
    assert hawkeye_user.original_accounts.count() == 1

    updated_resource = service_client.get_resource(str(hawkeye_user.resource.ansible_id)).json()
    assert updated_resource
    assert updated_resource["is_partially_migrated"] is False

    assert not User.objects.filter(username="already_migrated").exists()

    assert hawkeye_user.resource.service_id != migration_service.service_cluster.service_id

    gateway_service_id = service_id()
    assert str(hawkeye_user.resource.service_id) == gateway_service_id


@pytest.mark.django_db(transaction=True)
def test_merge_users(migration_service, admin_user, admin_api_client, patched_resource_client, patched_load_rbac):
    from aap_gateway_api.models import ServiceCluster, ServiceType

    fake_service_type = ServiceType.objects.create(name="fake")
    fake_service_cluster = ServiceCluster.objects.create(name="fake", service_type=fake_service_type)

    u = User.objects.create(username="hawkeye", email="hawkeye@secretbase.invalid")
    MigratedUserMetadata.objects.create(user=u, service=fake_service_cluster, original_username="hawkeye")

    service_client = patched_resource_client(service=migration_service, user=admin_user, raise_if_bad_request=True)

    call_command("migrate_service_data", username=admin_user.username)
    assert_all_resources_synced(admin_api_client, migration_service, service_client)

    assert User.objects.filter(username="hawkeye").exists()

    assert not User.objects.filter(username=f'{service_client.service.api_slug}{SEP_CHAR}hawkeye').exists()

    hawkeye_user = User.objects.get(username="hawkeye")
    assert hawkeye_user.original_accounts.count() == 1

    updated_resource = service_client.get_resource(str(hawkeye_user.resource.ansible_id)).json()
    assert updated_resource
    assert updated_resource["is_partially_migrated"] is False

    updated_user = updated_resource['resource_data']
    assert updated_user.get('username') == 'hawkeye', updated_user

    assert not User.objects.filter(username="already_migrated").exists()


@pytest.mark.django_db(transaction=True)
def test_correcting_user_service_id(migration_service, admin_user, patched_resource_client, patched_load_rbac):
    """Verify that a service user resource with the same ansible id but a differing
    service id from gateway's has its service id corrected to gateway's via migration.
    """
    call_command("migrate_service_data", username=admin_user.username)

    service_client = patched_resource_client(service=migration_service, user=admin_user, raise_if_bad_request=True)

    response = service_client.list_resources(filters={"name": "fury"})
    assert response.status_code == 200
    result = response.json()
    assert result["count"] == 1
    service_fury_resource_data = result["results"][0]
    service_client.update_resource(
        service_fury_resource_data["ansible_id"],
        ResourceRequestBody(**{"is_partially_migrated": False}),
        partial=True,
    )

    gw_fury_resource = Resource.objects.get(name="fury")
    gw_fury_resource.service_id = service_id()
    gw_fury_resource.save(update_fields=["service_id"])

    call_command("migrate_service_data", username=admin_user.username)

    response = service_client.list_resources(filters={"name": "fury"})
    assert response.status_code == 200
    result = response.json()
    assert result["count"] == 1
    service_fury_resource_data = result["results"][0]
    assert service_fury_resource_data["service_id"] == service_id()


@pytest.mark.django_db(transaction=True)
def test_migrating_user_with_invalid_email(migration_service_invalid_users, admin_user, patched_load_rbac):
    cmd = MigrateCommand()
    cmd.service_slug = 'controller'

    call_command(cmd, username=admin_user.username)

    users = User.objects.filter(username="bademailuser1")
    assert users is not None and users.exists()
    for u in users:
        assert u.first_name == "Badema"
        assert u.last_name == "Iluser"
        assert u.email == ""


@pytest.mark.django_db(transaction=True)
def test_updating_resource_data_for_invalid_resource(migration_service_invalid_users, patched_load_rbac, admin_user):
    from django.core.management.base import CommandError

    with patch.object(MigrateCommand, "update_resource_data") as mocked:
        mocked.return_value = None

        cmd = MigrateCommand()
        cmd.service_slug = 'controller'

        with pytest.raises(CommandError):
            call_command(cmd, username=admin_user.username)

        assert not User.objects.filter(username="invaliduser").exists()
        assert not User.objects.filter(username="bademailuser1").exists()


@pytest.mark.django_db
def test_process_migrate_resource_item_raises_on_missing_resource_data():
    """Test that _process_and_migrate_resource_item raises when resource_data is missing."""
    cmd = MigrateCommand()
    resource_item = {"ansible_id": "test-id-123", "name": "test"}
    resource_context = {"type": Mock()}

    with pytest.raises(RuntimeError, match="missing 'resource_data'"):
        cmd._process_and_migrate_resource_item(resource_item, resource_context)


@pytest.mark.django_db
def test_reconcile_existing_resource_matching_ansible_id_same_data():
    """Case 1 with matching data: logs 'Correcting service_id'."""
    from ansible_base.resource_registry.models import ResourceType

    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()

    resource_type = ResourceType.objects.get(name="shared.organization")
    org = Organization.objects.create(name="reconcile-org")
    resource = Resource.objects.get(content_type=resource_type.content_type, object_id=org.pk)
    local_data = resource_type.serializer_class(org).data

    resource_context = {
        "type": resource_type,
        "unique_fields": ["name"],
        "LocalResourceModel": Organization,
    }
    upstream_resource = {
        "ansible_id": str(resource.ansible_id),
        "name": org.name,
        "resource_data": local_data,
    }
    updated_service_resource = {}

    result = cmd._reconcile_existing_resource(upstream_resource, resource_context, local_data, updated_service_resource)

    assert result is False
    assert "Correcting service_id" in cmd.stdout.getvalue()


@pytest.mark.django_db
def test_reconcile_existing_resource_matching_ansible_id_different_data():
    """Case 1 with different data: logs 'Updating already-merged' and overwrites resource_data."""
    from ansible_base.resource_registry.models import ResourceType

    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()

    resource_type = ResourceType.objects.get(name="shared.organization")
    org = Organization.objects.create(name="reconcile-org-diff")
    resource = Resource.objects.get(content_type=resource_type.content_type, object_id=org.pk)
    local_data = resource_type.serializer_class(org).data

    resource_context = {
        "type": resource_type,
        "unique_fields": ["name"],
        "LocalResourceModel": Organization,
    }
    upstream_resource = {
        "ansible_id": str(resource.ansible_id),
        "name": org.name,
        "resource_data": {**local_data, "description": "stale upstream copy"},
    }
    updated_service_resource = {}

    result = cmd._reconcile_existing_resource(upstream_resource, resource_context, local_data, updated_service_resource)

    assert result is False
    assert updated_service_resource["resource_data"] == local_data
    combined_output = cmd.stdout.getvalue() + cmd.stderr.getvalue()
    assert "Updating already-merged" in combined_output


# ======================================================================# use_controller_password tests (2.6-specific)
# ======================================================================


def test_set_use_controller_password_flag_existing_user(capsys):
    """Test that _set_use_controller_password_flag sets the flag on existing users"""

    # Create a user without the flag set
    test_user = User.objects.create(username="test_user", use_controller_password=False)
    assert test_user.use_controller_password is False

    # Create upstream resource data
    upstream_resource = {
        "resource_data": {
            "username": "test_user",
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
        }
    }

    cmd = MigrateCommand()
    result = cmd._set_use_controller_password_flag(upstream_resource)

    # Verify the flag was set
    test_user.refresh_from_db()
    assert test_user.use_controller_password is True

    # Verify return value
    assert result == upstream_resource

    # Verify logging
    captured = capsys.readouterr()
    assert "Set use_controller_password flag for Gateway user 'test_user'" in captured.out


@pytest.mark.parametrize(
    "use_controller_password,password,last_login,expected_use_controller_password",
    [
        # expected to change; all critera met
        (False, None, None, True),
        # expected to not change; has last login
        (False, None, "2025-10-13T22:37:47.639930Z", False),
        # expected to not change; has password
        (False, "password", None, False),
        # expected to not change; has password, has last login
        (False, "password", "2025-10-13T22:37:47.639930Z", False),
        # expected to not change; has use_controller_password set
        (True, None, None, True),
        # expected to not change; has use_controller_password set, has last login
        (True, None, "2025-10-13T22:37:47.639930Z", True),
        # expected to not change; has use_controller_password set, has passwprd
        (True, "password", None, True),
        # expected to not change; has use_controller_password set, has password, has last login
        (True, "password", "2025-10-13T22:37:47.639930Z", True),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_use_controller_password_flag_correction_for_existing_users(
    use_controller_password,
    password,
    last_login,
    expected_use_controller_password,
    admin_user,
    service_api_route_controller,
):
    """Test that use_controller_password flag is apprpriateley corrected for existing users"""

    # In the case of a 2.4 to 2.5 upgrade after which a user did not login
    # before a follow-on upgrade to 2.6 the user is in a state such that
    # login is impossible.  The migration processing now includes a recovery
    # stage to address this.
    #
    # The recovery stage queries controller for users that have the service id of gateway
    # and processes them as such... if the user meets all of the following criteria
    #
    #   use_controller_password is False
    #   last_login does not exist
    #   password is not usable
    #
    # then the user has uers_controller_password set True.
    #
    # If any condition is not met the user is not modified.
    #
    # For the purposes of this test we mock the results of querying controller.

    # Create an existing user in Gateway with the test settings.
    existing_user = User.objects.create(
        username="existing_user",
        use_controller_password=use_controller_password,
        password=password,
    )
    if last_login:
        existing_user.last_login = last_login
        existing_user.save()

    # Mock the resource client for controller
    with (
        patch('aap_gateway_api.utils.resources_client.GWResourceAPIClient') as mock_client_class,
        patch('aap_gateway_api.utils.jwt_token.create_signed_jwt') as mock_jwt,
        patch('aap_gateway_api.utils.jwt_token.get_jwt_rsa_key') as mock_key,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._ensure_superuser_consistency') as mock_consistency_check,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._merge_partially_migrated_users') as mock_merge_users,
    ):
        # Mock JWT and key creation
        mock_jwt.return_value = 'fake-jwt-token'
        mock_key.return_value = 'fake-key'
        mock_consistency_check.return_value = None
        mock_merge_users.return_value = None

        mock_client = Mock()
        setup_basic_service_client_mocks(mock_client, service_api_route_controller, admin_user)

        # Mock a user resource response.
        # It doesn't have to have the same ansible id, but it's okay if it it.
        mock_user_resource = {
            "ansible_id": existing_user.resource.ansible_id,
            "name": "existing_user",
            "resource_data": {
                "username": "existing_user",
                "email": "existing@example.com",
                "first_name": "Existing",
                "last_name": "User",
                "is_superuser": False,
            },
            "resource_type": "shared.user",
        }

        # Mock the API responses with call counter to simulate resource processing
        # Migration processes orgs, teams, then users - we need to mock all of them
        call_counts = {"shared.organization": 0, "shared.team": 0, "shared.user": 0}

        def mock_list_resources_response(*args, **kwargs):
            filters = kwargs.get('filters', {})
            resource_type = filters.get('content_type__resource_type__name')

            if resource_type == 'shared.user':
                # Call 1: _is_service_already_synced (return unmigrated so migration proceeds)
                # Call 2: correction processing query (return the user resource)
                # Call 3+: migration loop (return empty)
                call_counts['shared.user'] += 1
                if call_counts['shared.user'] <= 2:
                    return Mock(json=lambda: {"count": 1, "results": [mock_user_resource]})
                else:
                    return Mock(json=lambda: {"count": 0, "results": []})
            else:
                # For organizations and teams, return empty immediately
                return Mock(json=lambda: {"count": 0, "results": []})

        mock_client.list_resources.side_effect = mock_list_resources_response
        mock_client.get_resource.return_value.json.return_value = mock_user_resource
        mock_client.update_resource.return_value = Mock()
        mock_client.list_user_assignments.return_value.json.return_value = {"count": 0, "results": []}
        mock_client.list_team_assignments.return_value.json.return_value = {"count": 0, "results": []}

        mock_client_class.return_value = mock_client

        # Run migration
        call_command("migrate_service_data", username=admin_user.username)

        # Verify that the existing user has use_controller_password=False
        existing_user.refresh_from_db()
        assert existing_user.use_controller_password is expected_use_controller_password


@pytest.mark.django_db(transaction=True)
def test_set_use_controller_password_flag_nonexistent_user(capsys):
    """Test that _set_use_controller_password_flag handles nonexistent users gracefully"""

    # Create upstream resource data for non-existent user
    upstream_resource = {
        "resource_data": {
            "username": "nonexistent_user",
            "email": "nonexistent@example.com",
            "first_name": "Non",
            "last_name": "Existent",
        }
    }

    cmd = MigrateCommand()
    result = cmd._set_use_controller_password_flag(upstream_resource)

    # Verify no user was created
    assert not User.objects.filter(username="nonexistent_user").exists()

    # Verify return value
    assert result == upstream_resource

    # Verify logging
    captured = capsys.readouterr()
    assert "Gateway user 'nonexistent_user' was not updated with 'use_controller_password' flag" in captured.err


@pytest.mark.django_db(transaction=True)
def test_use_controller_password_flag_integration_with_migration(admin_user, capsys, service_api_route_controller):
    """Test integration of use_controller_password flag setting during full migration"""

    # Mock the resource client for controller
    with (
        patch('aap_gateway_api.utils.resources_client.GWResourceAPIClient') as mock_client_class,
        patch('aap_gateway_api.utils.jwt_token.create_signed_jwt') as mock_jwt,
        patch('aap_gateway_api.utils.jwt_token.get_jwt_rsa_key') as mock_key,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._ensure_superuser_consistency') as mock_consistency_check,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._merge_partially_migrated_users') as mock_merge_users,
    ):
        # Mock JWT and key creation
        mock_jwt.return_value = 'fake-jwt-token'
        mock_key.return_value = 'fake-key'
        mock_consistency_check.return_value = None
        mock_merge_users.return_value = None

        mock_client = Mock()
        setup_basic_service_client_mocks(mock_client, service_api_route_controller, admin_user)

        # Mock a user resource to be migrated
        mock_user_resource = {
            "ansible_id": str(uuid.uuid4()),
            "name": "controller_user",
            "resource_data": {
                "username": "controller_user",
                "email": "controller@example.com",
                "first_name": "Controller",
                "last_name": "User",
                "is_superuser": False,
            },
            "resource_type": "shared.user",
        }

        # Mock the API responses with call counter to simulate resource processing
        # Migration processes orgs, teams, then users - we need to mock all of them
        call_counts = {"shared.organization": 0, "shared.team": 0, "shared.user": 0}

        def mock_list_resources_response(*args, **kwargs):
            filters = kwargs.get('filters', {})
            resource_type = filters.get('content_type__resource_type__name')

            if resource_type == 'shared.user':
                call_counts['shared.user'] += 1
                # Call 1: _is_service_already_synced (return unmigrated so migration proceeds)
                # Call 2: correction stage (return empty — we don't want correction here)
                # Call 3: migration loop (return the user resource to be migrated)
                # Call 4+: migration loop continuation (return empty)
                if call_counts['shared.user'] in (1, 3):
                    return Mock(json=lambda: {"count": 1, "results": [mock_user_resource]})
                else:
                    return Mock(json=lambda: {"count": 0, "results": []})
            else:
                # For organizations and teams, return empty immediately
                return Mock(json=lambda: {"count": 0, "results": []})

        mock_client.list_resources.side_effect = mock_list_resources_response
        mock_client.get_resource.return_value.json.return_value = mock_user_resource
        mock_client.update_resource.return_value = Mock()
        mock_client.list_user_assignments.return_value.json.return_value = {"count": 0, "results": []}
        mock_client.list_team_assignments.return_value.json.return_value = {"count": 0, "results": []}

        mock_client_class.return_value = mock_client

        # Run migration
        call_command("migrate_service_data", username=admin_user.username)

        # Verify that the user was created with use_controller_password=True
        assert User.objects.filter(username="controller_user").exists()
        controller_user = User.objects.get(username="controller_user")
        assert controller_user.use_controller_password is True

        # Verify logging
        captured = capsys.readouterr()
        assert "Set use_controller_password flag for Gateway user 'controller_user'" in captured.out


@pytest.mark.django_db(transaction=True)
def test_use_controller_password_flag_only_for_user_resources(admin_user, service_api_route_controller):
    """Test that use_controller_password flag is only set for user resources, not organizations or teams"""

    # Mock the resource client for controller
    with (
        patch('aap_gateway_api.utils.resources_client.GWResourceAPIClient') as mock_client_class,
        patch('aap_gateway_api.utils.jwt_token.create_signed_jwt') as mock_jwt,
        patch('aap_gateway_api.utils.jwt_token.get_jwt_rsa_key') as mock_key,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._ensure_superuser_consistency') as mock_consistency_check,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._merge_partially_migrated_users') as mock_merge_users,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._set_use_controller_password_flag') as mock_set_flag,
    ):
        # Mock JWT and key creation
        mock_jwt.return_value = 'fake-jwt-token'
        mock_key.return_value = 'fake-key'
        mock_consistency_check.return_value = None
        mock_merge_users.return_value = None

        mock_client = Mock()
        setup_basic_service_client_mocks(mock_client, service_api_route_controller, admin_user)

        # Mock organization and user resources
        mock_org_resource = {
            "ansible_id": str(uuid.uuid4()),
            "name": "test_org",
            "resource_data": {"name": "test_org"},
            "resource_type": "shared.organization",
        }

        mock_user_resource = {
            "ansible_id": str(uuid.uuid4()),
            "name": "test_user",
            "resource_data": {
                "username": "test_user",
                "email": "user@example.com",
                "first_name": "Test",
                "last_name": "User",
                "is_superuser": False,
            },
            "resource_type": "shared.user",
        }

        # Mock multiple list_resources calls for different resource types
        # Use a call counter to simulate resources being processed and disappearing from subsequent queries
        call_counts = {"shared.organization": 0, "shared.user": 0}

        def mock_list_resources(*args, **kwargs):
            filters = kwargs.get('filters', {})
            resource_type = filters.get('content_type__resource_type__name')

            if resource_type == 'shared.organization':
                call_counts["shared.organization"] += 1
                if call_counts["shared.organization"] == 1:
                    return Mock(json=lambda: {"count": 1, "results": [mock_org_resource]})
                else:
                    return Mock(json=lambda: {"count": 0, "results": []})
            elif resource_type == 'shared.user':
                # The processing now includes a recovery stage to handle
                # 2.4 -> 2.5 upgrades after which a user did not login followed
                # by an upgrade to 2.6 which would make it imposible for the user
                # to login using the controller password.
                # For the purposes of this test we do not want the mocking of
                # list_resources to return the user on the first (correction stage)
                # request.
                call_counts["shared.user"] += 1
                if call_counts["shared.user"] == 2:
                    return Mock(json=lambda: {"count": 1, "results": [mock_user_resource]})
                else:
                    return Mock(json=lambda: {"count": 0, "results": []})
            else:
                return Mock(json=lambda: {"count": 0, "results": []})

        mock_client.list_resources.side_effect = mock_list_resources

        def mock_get_resource(ansible_id):
            if ansible_id == mock_org_resource["ansible_id"]:
                return Mock(json=lambda: mock_org_resource)
            elif ansible_id == mock_user_resource["ansible_id"]:
                return Mock(json=lambda: mock_user_resource)
            return Mock(json=lambda: {})

        mock_client.get_resource.side_effect = mock_get_resource
        mock_client.update_resource.return_value = Mock()
        mock_client.list_user_assignments.return_value.json.return_value = {"count": 0, "results": []}
        mock_client.list_team_assignments.return_value.json.return_value = {"count": 0, "results": []}

        mock_client_class.return_value = mock_client

        # Run migration
        call_command("migrate_service_data", username=admin_user.username)

        # Verify _set_use_controller_password_flag was called only for user resources
        # It should be called once for the user resource, not for the organization
        assert mock_set_flag.call_count == 1

        # Verify it was called with the user resource
        call_args = mock_set_flag.call_args[0][0]  # First argument of the first call
        assert call_args["resource_data"]["username"] == "test_user"


@pytest.mark.django_db(transaction=True)
def test_use_controller_password_flag_not_set_for_existing_users_during_merge(admin_user, service_api_route_controller):
    """Test that use_controller_password flag handling works correctly when merging with existing users"""
    # Create an existing user in Gateway (simulating a conflict scenario)
    existing_user = User.objects.create(username="existing_user", use_controller_password=False)

    # Mock the resource client for controller
    with (
        patch('aap_gateway_api.utils.resources_client.GWResourceAPIClient') as mock_client_class,
        patch('aap_gateway_api.utils.jwt_token.create_signed_jwt') as mock_jwt,
        patch('aap_gateway_api.utils.jwt_token.get_jwt_rsa_key') as mock_key,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._ensure_superuser_consistency') as mock_consistency_check,
        patch('aap_gateway_api.management.commands.migrate_service_data.Command._merge_partially_migrated_users') as mock_merge_users,
    ):
        # Mock JWT and key creation
        mock_jwt.return_value = 'fake-jwt-token'
        mock_key.return_value = 'fake-key'
        mock_consistency_check.return_value = None
        mock_merge_users.return_value = None

        mock_client = Mock()
        setup_basic_service_client_mocks(mock_client, service_api_route_controller, admin_user)

        # Mock a user resource that will conflict with existing user
        mock_user_resource = {
            "ansible_id": existing_user.resource.ansible_id,  # Same ansible_id as existing user
            "name": "existing_user",
            "resource_data": {
                "username": "existing_user",
                "email": "existing@example.com",
                "first_name": "Existing",
                "last_name": "User",
                "is_superuser": False,
            },
            "resource_type": "shared.user",
        }

        # Mock the API responses with call counter to simulate resource processing
        # Migration processes orgs, teams, then users - we need to mock all of them
        call_counts = {"shared.organization": 0, "shared.team": 0, "shared.user": 0}

        def mock_list_resources_response(*args, **kwargs):
            filters = kwargs.get('filters', {})
            resource_type = filters.get('content_type__resource_type__name')

            if resource_type == 'shared.user':
                # The processing now includes a recovery stage to handle
                # 2.4 -> 2.5 upgrades after which a user did not login followed
                # by an upgrade to 2.6 which would make it imposible for the user
                # to login using the controller password.
                # For the purposes of this test we do not want the mocking of
                # list_resources to return the user on the first (correction stage)
                # request.
                call_counts['shared.user'] += 1
                if call_counts['shared.user'] == 2:
                    return Mock(json=lambda: {"count": 1, "results": [mock_user_resource]})
                else:
                    return Mock(json=lambda: {"count": 0, "results": []})
            else:
                # For organizations and teams, return empty immediately
                return Mock(json=lambda: {"count": 0, "results": []})

        mock_client.list_resources.side_effect = mock_list_resources_response
        mock_client.get_resource.return_value.json.return_value = mock_user_resource
        mock_client.update_resource.return_value = Mock()
        mock_client.list_user_assignments.return_value.json.return_value = {"count": 0, "results": []}
        mock_client.list_team_assignments.return_value.json.return_value = {"count": 0, "results": []}

        mock_client_class.return_value = mock_client

        # Run migration
        call_command("migrate_service_data", username=admin_user.username)

        # Verify that the existing user has use_controller_password=False
        existing_user.refresh_from_db()
        assert existing_user.use_controller_password is False


@pytest.mark.django_db(transaction=True)
def test_use_controller_password_save_uses_update_fields():
    """Test that _set_use_controller_password_flag uses update_fields for efficient saves"""

    # Create a user without the flag set
    User.objects.create(username="test_user", use_controller_password=False)

    # Create upstream resource data
    upstream_resource = {
        "resource_data": {
            "username": "test_user",
            "email": "test@example.com",
            "first_name": "Test",
            "last_name": "User",
        }
    }

    cmd = MigrateCommand()

    # Patch the save method to verify update_fields is used
    with patch.object(User, 'save') as mock_save:
        cmd._set_use_controller_password_flag(upstream_resource)

        # Verify save was called with update_fields
        mock_save.assert_called_once_with(update_fields=["use_controller_password"])


def test_deserialize_and_validate_resource_data_valid():
    """When the serializer reports valid data, the validated_data dict is returned directly."""
    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()

    validated = {"username": "tony", "email": "tony@stark.invalid"}
    mock_serializer_instance = Mock()
    mock_serializer_instance.is_valid.return_value = True
    mock_serializer_instance.validated_data = validated

    mock_serializer_cls = Mock(return_value=mock_serializer_instance)

    upstream_resource = {
        "ansible_id": "test-aid-001",
        "resource_type": "shared.user",
        "resource_data": {"username": "tony", "email": "tony@stark.invalid"},
    }

    result = cmd._deserialize_and_validate_resource_data(upstream_resource, mock_serializer_cls)

    assert result == validated
    mock_serializer_cls.assert_called_once_with(data=upstream_resource["resource_data"])
    mock_serializer_instance.is_valid.assert_called_once_with(raise_exception=False)


def test_deserialize_and_validate_resource_data_invalid_then_fixed():
    """When validation fails but update_resource_data returns a fix, the fixed data is returned."""
    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()

    fixed_data = {"username": "baduser", "email": ""}

    mock_serializer_instance = Mock()
    mock_serializer_instance.is_valid.return_value = False
    mock_serializer_instance.errors = {"email": ["Enter a valid email address."]}
    mock_serializer_instance.data = {"username": "baduser", "email": "not-an-email"}

    mock_serializer_cls = Mock(return_value=mock_serializer_instance)

    upstream_resource = {
        "ansible_id": "test-aid-002",
        "resource_type": "shared.user",
        "resource_data": {"username": "baduser", "email": "not-an-email"},
    }

    with patch.object(MigrateCommand, "update_resource_data", return_value=fixed_data):
        result = cmd._deserialize_and_validate_resource_data(upstream_resource, mock_serializer_cls)

    assert result == fixed_data
    # The upstream_resource should have its resource_data updated to the fixed data
    assert upstream_resource["resource_data"] == fixed_data


def test_deserialize_and_validate_resource_data_invalid_unfixable():
    """When validation fails and update_resource_data returns None, RuntimeError is raised."""
    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()

    mock_serializer_instance = Mock()
    mock_serializer_instance.is_valid.return_value = False
    mock_serializer_instance.errors = {"username": ["This field is required."]}
    mock_serializer_instance.data = {}

    mock_serializer_cls = Mock(return_value=mock_serializer_instance)

    upstream_resource = {
        "ansible_id": "test-aid-003",
        "resource_type": "shared.user",
        "resource_data": {},
    }

    with patch.object(MigrateCommand, "update_resource_data", return_value=None):
        with pytest.raises(RuntimeError, match="invalid, non-correctable"):
            cmd._deserialize_and_validate_resource_data(upstream_resource, mock_serializer_cls)


def test_initialize_resource_sync_payloads():
    """Payloads contain the upstream ansible_id and the gateway service_id."""
    cmd = MigrateCommand()

    upstream_resource = {
        "ansible_id": "test-aid-100",
        "resource_data": {"username": "pepper"},
    }

    with patch("aap_gateway_api.management.commands._migrate_service_data.resource_migration.service_id", return_value="gw-service-id-42"):
        creation_kwargs, service_resource = cmd._initialize_resource_sync_payloads(upstream_resource)

    assert creation_kwargs == {"ansible_id": "test-aid-100"}
    assert service_resource == {"service_id": "gw-service-id-42"}


def test_get_filtered_resources_excludes_system_user():
    """For shared.user resources, the system user (settings.SYSTEM_USERNAME) is excluded."""
    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()
    cmd.RESOURCE_DATA_FILTERS = {"extra_fields": "resource_data"}

    system_username = "_system"
    mock_response = Mock()
    mock_response.json.return_value = {
        "count": 3,
        "results": [
            {"name": "tony", "ansible_id": "a1"},
            {"name": system_username, "ansible_id": "a2"},
            {"name": "pepper", "ansible_id": "a3"},
        ],
    }

    cmd.client = Mock()
    cmd.client.list_resources.return_value = mock_response

    with patch("aap_gateway_api.management.commands._migrate_service_data.resource_migration.settings") as mock_settings:
        mock_settings.SYSTEM_USERNAME = system_username
        results, count = cmd._get_filtered_resources({}, "shared.user")

    assert count == 3
    assert len(results) == 2
    names = [r["name"] for r in results]
    assert system_username not in names
    assert "tony" in names
    assert "pepper" in names


def test_get_filtered_resources_non_user_type():
    """For non-user resource types, no filtering is applied and all results are returned."""
    cmd = MigrateCommand()
    cmd.stdout = StringIO()
    cmd.stderr = StringIO()
    cmd.RESOURCE_DATA_FILTERS = {"extra_fields": "resource_data"}

    mock_response = Mock()
    mock_response.json.return_value = {
        "count": 2,
        "results": [
            {"name": "Org1", "ansible_id": "o1"},
            {"name": "Org2", "ansible_id": "o2"},
        ],
    }

    cmd.client = Mock()
    cmd.client.list_resources.return_value = mock_response

    results, count = cmd._get_filtered_resources({}, "shared.organization")

    assert count == 2
    assert len(results) == 2
    assert results[0]["name"] == "Org1"
    assert results[1]["name"] == "Org2"
