from contextlib import contextmanager
from datetime import datetime
from functools import partial
import ConfigParser
import json
import tempfile

import os
import fcntl
import pytest
import mock
import yaml

from awx.main.models import (
    Credential,
    CredentialType,
    Inventory,
    InventorySource,
    InventoryUpdate,
    Job,
    Notification,
    Project,
    ProjectUpdate,
    UnifiedJob,
)

from awx.main import tasks
from awx.main.task_engine import TaskEnhancer
from awx.main.utils.common import encrypt_field


@contextmanager
def apply_patches(_patches):
    [p.start() for p in _patches]
    yield
    [p.stop() for p in _patches]


def test_send_notifications_not_list():
    with pytest.raises(TypeError):
        tasks.send_notifications(None)


def test_send_notifications_job_id(mocker):
    with mocker.patch('awx.main.models.UnifiedJob.objects.get'):
        tasks.send_notifications([], job_id=1)
        assert UnifiedJob.objects.get.called
        assert UnifiedJob.objects.get.called_with(id=1)


def test_work_success_callback_missing_job():
    task_data = {'type': 'project_update', 'id': 9999}
    with mock.patch('django.db.models.query.QuerySet.get') as get_mock:
        get_mock.side_effect = ProjectUpdate.DoesNotExist()
        assert tasks.handle_work_success(None, task_data) is None


def test_send_notifications_list(mocker):
    patches = list()

    mock_job = mocker.MagicMock(spec=UnifiedJob)
    patches.append(mocker.patch('awx.main.models.UnifiedJob.objects.get', return_value=mock_job))

    mock_notifications = [mocker.MagicMock(spec=Notification, subject="test", body={'hello': 'world'})]
    patches.append(mocker.patch('awx.main.models.Notification.objects.filter', return_value=mock_notifications))

    with apply_patches(patches):
        tasks.send_notifications([1,2], job_id=1)
        assert Notification.objects.filter.call_count == 1
        assert mock_notifications[0].status == "successful"
        assert mock_notifications[0].save.called

        assert mock_job.notifications.add.called
        assert mock_job.notifications.add.called_with(*mock_notifications)


@pytest.mark.parametrize("current_instances,call_count", [(91, 2), (89,1)])
def test_run_admin_checks_usage(mocker, current_instances, call_count):
    patches = list()
    patches.append(mocker.patch('awx.main.tasks.User'))

    mock_te = mocker.Mock(spec=TaskEnhancer)
    mock_te.validate_enhancements.return_value = {'instance_count': 100, 'current_instances': current_instances, 'date_warning': True}
    patches.append(mocker.patch('awx.main.tasks.TaskEnhancer', return_value=mock_te))

    mock_sm = mocker.Mock()
    patches.append(mocker.patch('awx.main.tasks.send_mail', wraps=mock_sm))

    with apply_patches(patches):
        tasks.run_administrative_checks()
        assert mock_sm.called
        if call_count == 2:
            assert '90%' in mock_sm.call_args_list[0][0][0]
        else:
            assert 'expire' in mock_sm.call_args_list[0][0][0]


@pytest.mark.parametrize("key,value", [
    ('REST_API_TOKEN', 'SECRET'),
    ('SECRET_KEY', 'SECRET'),
    ('RABBITMQ_PASS', 'SECRET'),
    ('VMWARE_PASSWORD', 'SECRET'),
    ('API_SECRET', 'SECRET'),
    ('CALLBACK_CONNECTION', 'amqp://tower:password@localhost:5672/tower'),
])
def test_safe_env_filtering(key, value):
    task = tasks.RunJob()
    assert task.build_safe_env({key: value})[key] == tasks.HIDDEN_PASSWORD


def test_safe_env_returns_new_copy():
    task = tasks.RunJob()
    env = {'foo': 'bar'}
    assert task.build_safe_env(env) is not env


def test_openstack_client_config_generation(mocker):
    update = tasks.RunInventoryUpdate()
    inventory_update = mocker.Mock(**{
        'source': 'openstack',
        'credential.host': 'https://keystone.openstack.example.org',
        'credential.username': 'demo',
        'credential.password': 'secrete',
        'credential.project': 'demo-project',
        'credential.domain': None,
        'source_vars_dict': {}
    })
    cloud_config = update.build_private_data(inventory_update)
    cloud_credential = yaml.load(cloud_config['cloud_credential'])
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {
                'auth_url': 'https://keystone.openstack.example.org',
                'password': 'secrete',
                'project_name': 'demo-project',
                'username': 'demo'
            },
            'private': True
        }
    }


@pytest.mark.parametrize("source,expected", [
    (False, False), (True, True)
])
def test_openstack_client_config_generation_with_private_source_vars(mocker, source, expected):
    update = tasks.RunInventoryUpdate()
    inventory_update = mocker.Mock(**{
        'source': 'openstack',
        'credential.host': 'https://keystone.openstack.example.org',
        'credential.username': 'demo',
        'credential.password': 'secrete',
        'credential.project': 'demo-project',
        'credential.domain': None,
        'source_vars_dict': {'private': source}
    })
    cloud_config = update.build_private_data(inventory_update)
    cloud_credential = yaml.load(cloud_config['cloud_credential'])
    assert cloud_credential['clouds'] == {
        'devstack': {
            'auth': {
                'auth_url': 'https://keystone.openstack.example.org',
                'password': 'secrete',
                'project_name': 'demo-project',
                'username': 'demo'
            },
            'private': expected
        }
    }


def pytest_generate_tests(metafunc):
    # pytest.mark.parametrize doesn't work on unittest.TestCase methods
    # see: https://docs.pytest.org/en/latest/example/parametrize.html#parametrizing-test-methods-through-per-class-configuration
    if metafunc.cls and hasattr(metafunc.cls, 'parametrize'):
        funcarglist = metafunc.cls.parametrize.get(metafunc.function.__name__)
        if funcarglist:
            argnames = sorted(funcarglist[0])
            metafunc.parametrize(
                argnames,
                [[funcargs[name] for name in argnames] for funcargs in funcarglist]
            )


class TestJobExecution:
    """
    For job runs, test that `ansible-playbook` is invoked with the proper
    arguments, environment variables, and pexpect passwords for a variety of
    credential types.
    """

    TASK_CLS = tasks.RunJob
    EXAMPLE_PRIVATE_KEY = '-----BEGIN PRIVATE KEY-----\nxyz==\n-----END PRIVATE KEY-----'

    def setup_method(self, method):
        self.patches = [
            mock.patch.object(Project, 'get_project_path', lambda *a, **kw: '/tmp/'),
            # don't emit websocket statuses; they use the DB and complicate testing
            mock.patch.object(UnifiedJob, 'websocket_emit_status', mock.Mock()),
            mock.patch.object(Job, 'inventory', mock.Mock(pk=1, spec_set=['pk']))
        ]
        for p in self.patches:
            p.start()

        self.instance = self.get_instance()

        def status_side_effect(pk, **kwargs):
            # If `Job.update_model` is called, we're not actually persisting
            # to the database; just update the status, which is usually
            # the update we care about for testing purposes
            if 'status' in kwargs:
                self.instance.status = kwargs['status']
            return self.instance

        self.task = self.TASK_CLS()
        self.task.update_model = mock.Mock(side_effect=status_side_effect)

        # The primary goal of these tests is to mock our `run_pexpect` call
        # and make assertions about the arguments and environment passed to it.
        self.task.run_pexpect = mock.Mock(return_value=['successful', 0])

        # ignore pre-run and post-run hooks, they complicate testing in a variety of ways
        self.task.pre_run_hook = self.task.post_run_hook = self.task.final_run_hook = mock.Mock()

    def teardown_method(self, method):
        for p in self.patches:
            p.stop()

    def get_instance(self):
        return Job(
            pk=1,
            created=datetime.utcnow(),
            status='new',
            job_type='run',
            cancel_flag=False,
            credential=None,
            cloud_credential=None,
            network_credential=None,
            project=Project()
        )

    @property
    def pk(self):
        return self.instance.pk


class TestGenericRun(TestJobExecution):

    def test_cancel_flag(self):
        self.instance.cancel_flag = True
        with pytest.raises(Exception):
            self.task.run(self.pk)
        for c in [
            mock.call(self.pk, celery_task_id='', status='running'),
            mock.call(self.pk, output_replacements=[], result_traceback=mock.ANY, status='canceled')
        ]:
            assert c in self.task.update_model.call_args_list

    def test_uses_bubblewrap(self):
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args
        assert args[0] == 'bwrap'


class TestJobCredentials(TestJobExecution):

    parametrize = {
        'test_ssh_passwords': [
            dict(field='password', password_name='ssh_password', expected_flag='--ask-pass'),
            dict(field='ssh_key_unlock', password_name='ssh_key_unlock', expected_flag=None),
            dict(field='become_password', password_name='become_password', expected_flag='--ask-become-pass'),
            dict(field='vault_password', password_name='vault_password', expected_flag='--ask-vault-pass'),
        ]
    }

    def test_ssh_passwords(self, field, password_name, expected_flag):
        ssh = CredentialType.defaults['ssh']()
        self.instance.credential = Credential(
            credential_type=ssh,
            inputs = {'username': 'bob', field: 'secret'}
        )
        self.instance.credential.inputs[field] = encrypt_field(
            self.instance.credential, field
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert passwords[password_name] == 'secret'
        assert '-u bob' in ' '.join(args)
        if expected_flag:
            assert expected_flag in ' '.join(args)

    def test_ssh_key_with_agent(self):
        ssh = CredentialType.defaults['ssh']()
        self.instance.credential = Credential(
            credential_type=ssh,
            inputs = {
                'username': 'bob',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(private_data, *args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            ssh_key_data_fifo = '/'.join([private_data, 'credential'])
            assert open(ssh_key_data_fifo, 'r').read() == self.EXAMPLE_PRIVATE_KEY
            assert ' '.join(args).startswith(
                'ssh-agent -a %s sh -c ssh-add %s && rm -f %s' % (
                    '/'.join([private_data, 'ssh_auth.sock']),
                    ssh_key_data_fifo,
                    ssh_key_data_fifo
                )
            )
            return ['successful', 0]

        private_data = tempfile.mkdtemp(prefix='ansible_tower_')
        self.task.build_private_data_dir = mock.Mock(return_value=private_data)
        self.task.run_pexpect = mock.Mock(
            side_effect=partial(run_pexpect_side_effect, private_data)
        )
        self.task.run(self.pk, private_data_dir=private_data)

    def test_aws_cloud_credential(self):
        aws = CredentialType.defaults['aws']()
        self.instance.cloud_credential = Credential(
            credential_type=aws,
            inputs = {'username': 'bob', 'password': 'secret'}
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['AWS_ACCESS_KEY'] == 'bob'
        assert env['AWS_SECRET_KEY'] == 'secret'
        assert 'AWS_SECURITY_TOKEN' not in env

    def test_aws_cloud_credential_with_sts_token(self):
        aws = CredentialType.defaults['aws']()
        self.instance.cloud_credential = Credential(
            credential_type=aws,
            inputs = {'username': 'bob', 'password': 'secret', 'security_token': 'token'}
        )
        for key in ('password', 'security_token'):
            self.instance.cloud_credential.inputs[key] = encrypt_field(
                self.instance.cloud_credential, key
            )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['AWS_ACCESS_KEY'] == 'bob'
        assert env['AWS_SECRET_KEY'] == 'secret'
        assert env['AWS_SECURITY_TOKEN'] == 'token'

    def test_rax_credential(self):
        rax = CredentialType.defaults['rackspace']()
        self.instance.cloud_credential = Credential(
            credential_type=rax,
            inputs = {'username': 'bob', 'password': 'secret'}
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['RAX_USERNAME'] == 'bob'
        assert env['RAX_API_KEY'] == 'secret'
        assert env['CLOUD_VERIFY_SSL'] == 'False'

    def test_gce_credentials(self):
        gce = CredentialType.defaults['gce']()
        self.instance.cloud_credential = Credential(
            credential_type=gce,
            inputs = {
                'username': 'bob',
                'project': 'some-project',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.cloud_credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.cloud_credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert env['GCE_EMAIL'] == 'bob'
            assert env['GCE_PROJECT'] == 'some-project'
            ssh_key_data = env['GCE_PEM_FILE_PATH']
            assert open(ssh_key_data, 'rb').read() == self.EXAMPLE_PRIVATE_KEY
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_azure_credentials(self):
        azure = CredentialType.defaults['azure']()
        self.instance.cloud_credential = Credential(
            credential_type=azure,
            inputs = {
                'username': 'bob',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.cloud_credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.cloud_credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert env['AZURE_SUBSCRIPTION_ID'] == 'bob'
            ssh_key_data = env['AZURE_CERT_PATH']
            assert open(ssh_key_data, 'rb').read() == self.EXAMPLE_PRIVATE_KEY
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_azure_rm_with_tenant(self):
        azure = CredentialType.defaults['azure_rm']()
        self.instance.cloud_credential = Credential(
            credential_type=azure,
            inputs = {
                'client': 'some-client',
                'secret': 'some-secret',
                'tenant': 'some-tenant',
                'subscription': 'some-subscription'
            }
        )
        self.instance.cloud_credential.inputs['secret'] = encrypt_field(
            self.instance.cloud_credential, 'secret'
        )

        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['AZURE_CLIENT_ID'] == 'some-client'
        assert env['AZURE_SECRET'] == 'some-secret'
        assert env['AZURE_TENANT'] == 'some-tenant'
        assert env['AZURE_SUBSCRIPTION_ID'] == 'some-subscription'

    def test_azure_rm_with_password(self):
        azure = CredentialType.defaults['azure_rm']()
        self.instance.cloud_credential = Credential(
            credential_type=azure,
            inputs = {
                'subscription': 'some-subscription',
                'username': 'bob',
                'password': 'secret'
            }
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )

        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['AZURE_SUBSCRIPTION_ID'] == 'some-subscription'
        assert env['AZURE_AD_USER'] == 'bob'
        assert env['AZURE_PASSWORD'] == 'secret'

    def test_vmware_credentials(self):
        vmware = CredentialType.defaults['vmware']()
        self.instance.cloud_credential = Credential(
            credential_type=vmware,
            inputs = {'username': 'bob', 'password': 'secret', 'host': 'https://example.org'}
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['VMWARE_USER'] == 'bob'
        assert env['VMWARE_PASSWORD'] == 'secret'
        assert env['VMWARE_HOST'] == 'https://example.org'

    def test_openstack_credentials(self):
        openstack = CredentialType.defaults['openstack']()
        self.instance.cloud_credential = Credential(
            credential_type=openstack,
            inputs = {
                'username': 'bob',
                'password': 'secret',
                'project': 'tenant-name',
                'host': 'https://keystone.example.org'
            }
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            shade_config = open(env['OS_CLIENT_CONFIG_FILE'], 'rb').read()
            assert shade_config == '\n'.join([
                'clouds:',
                '  devstack:',
                '    auth:',
                '      auth_url: https://keystone.example.org',
                '      password: secret',
                '      project_name: tenant-name',
                '      username: bob',
                ''
            ])
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_net_credentials(self):
        net = CredentialType.defaults['net']()
        self.instance.network_credential = Credential(
            credential_type=net,
            inputs = {
                'username': 'bob',
                'password': 'secret',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY,
                'authorize': True,
                'authorize_password': 'authorizeme'
            }
        )
        for field in ('password', 'ssh_key_data', 'authorize_password'):
            self.instance.network_credential.inputs[field] = encrypt_field(
                self.instance.network_credential, field
            )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert env['ANSIBLE_NET_USERNAME'] == 'bob'
            assert env['ANSIBLE_NET_PASSWORD'] == 'secret'
            assert env['ANSIBLE_NET_AUTHORIZE'] == '1'
            assert env['ANSIBLE_NET_AUTH_PASS'] == 'authorizeme'
            assert open(env['ANSIBLE_NET_SSH_KEYFILE'], 'rb').read() == self.EXAMPLE_PRIVATE_KEY
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_custom_environment_injectors_with_jinja_syntax_error(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'api_token',
                    'label': 'API Token',
                    'type': 'string'
                }]
            },
            injectors={
                'env': {
                    'MY_CLOUD_API_TOKEN': '{{api_token.foo()}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'api_token': 'ABC123'}
        )
        with pytest.raises(Exception):
            self.task.run(self.pk)

    def test_custom_environment_injectors(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'api_token',
                    'label': 'API Token',
                    'type': 'string'
                }]
            },
            injectors={
                'env': {
                    'MY_CLOUD_API_TOKEN': '{{api_token}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'api_token': 'ABC123'}
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['MY_CLOUD_API_TOKEN'] == 'ABC123'

    def test_custom_environment_injectors_with_reserved_env_var(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'api_token',
                    'label': 'API Token',
                    'type': 'string'
                }]
            },
            injectors={
                'env': {
                    'JOB_ID': 'reserved'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'api_token': 'ABC123'}
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['JOB_ID'] == str(self.instance.pk)

    def test_custom_environment_injectors_with_secret_field(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'password',
                    'label': 'Password',
                    'type': 'string',
                    'secret': True
                }]
            },
            injectors={
                'env': {
                    'MY_CLOUD_PRIVATE_VAR': '{{password}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'password': 'SUPER-SECRET-123'}
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert env['MY_CLOUD_PRIVATE_VAR'] == 'SUPER-SECRET-123'
        assert 'SUPER-SECRET-123' not in json.dumps(self.task.update_model.call_args_list)

    def test_custom_environment_injectors_with_extra_vars(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'api_token',
                    'label': 'API Token',
                    'type': 'string'
                }]
            },
            injectors={
                'extra_vars': {
                    'api_token': '{{api_token}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'api_token': 'ABC123'}
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert '-e {"api_token": "ABC123"}' in ' '.join(args)

    def test_custom_environment_injectors_with_secret_extra_vars(self):
        """
        extra_vars that contain secret field values should be censored in the DB
        """
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'password',
                    'label': 'Password',
                    'type': 'string',
                    'secret': True
                }]
            },
            injectors={
                'extra_vars': {
                    'password': '{{password}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'password': 'SUPER-SECRET-123'}
        )
        self.instance.cloud_credential.inputs['password'] = encrypt_field(
            self.instance.cloud_credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert '-e {"password": "SUPER-SECRET-123"}' in ' '.join(args)
        assert 'SUPER-SECRET-123' not in json.dumps(self.task.update_model.call_args_list)

    def test_custom_environment_injectors_with_file(self):
        some_cloud = CredentialType(
            kind='cloud',
            name='SomeCloud',
            managed_by_tower=False,
            inputs={
                'fields': [{
                    'id': 'api_token',
                    'label': 'API Token',
                    'type': 'string'
                }]
            },
            injectors={
                'file': {
                    'template': '[mycloud]\n{{api_token}}'
                },
                'env': {
                    'MY_CLOUD_INI_FILE': '{{tower.filename}}'
                }
            }
        )
        self.instance.cloud_credential = Credential(
            credential_type=some_cloud,
            inputs = {'api_token': 'ABC123'}
        )
        self.task.run(self.pk)

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert open(env['MY_CLOUD_INI_FILE'], 'rb').read() == '[mycloud]\nABC123'
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)


class TestProjectUpdateCredentials(TestJobExecution):

    TASK_CLS = tasks.RunProjectUpdate

    def get_instance(self):
        return ProjectUpdate(
            pk=1,
            project=Project()
        )

    parametrize = {
        'test_username_and_password_auth': [
            dict(scm_type='git'),
            dict(scm_type='hg'),
            dict(scm_type='svn'),
        ],
        'test_ssh_key_auth': [
            dict(scm_type='git'),
            dict(scm_type='hg'),
            dict(scm_type='svn'),
        ]
    }

    def test_username_and_password_auth(self, scm_type):
        ssh = CredentialType.defaults['ssh']()
        self.instance.scm_type = scm_type
        self.instance.credential = Credential(
            credential_type=ssh,
            inputs = {'username': 'bob', 'password': 'secret'}
        )
        self.instance.credential.inputs['password'] = encrypt_field(
            self.instance.credential, 'password'
        )
        self.task.run(self.pk)

        assert self.task.run_pexpect.call_count == 1
        call_args, _ = self.task.run_pexpect.call_args_list[0]
        job, args, cwd, env, passwords, stdout = call_args

        assert passwords.get('scm_username') == 'bob'
        assert passwords.get('scm_password') == 'secret'

    def test_ssh_key_auth(self, scm_type):
        ssh = CredentialType.defaults['ssh']()
        self.instance.scm_type = scm_type
        self.instance.credential = Credential(
            credential_type=ssh,
            inputs = {
                'username': 'bob',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(private_data, *args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            ssh_key_data_fifo = '/'.join([private_data, 'scm_credential'])
            assert open(ssh_key_data_fifo, 'r').read() == self.EXAMPLE_PRIVATE_KEY
            assert ' '.join(args).startswith(
                'ssh-agent -a %s sh -c ssh-add %s && rm -f %s' % (
                    '/'.join([private_data, 'ssh_auth.sock']),
                    ssh_key_data_fifo,
                    ssh_key_data_fifo
                )
            )
            assert passwords.get('scm_username') == 'bob'
            return ['successful', 0]

        private_data = tempfile.mkdtemp(prefix='ansible_tower_')
        self.task.build_private_data_dir = mock.Mock(return_value=private_data)
        self.task.run_pexpect = mock.Mock(
            side_effect=partial(run_pexpect_side_effect, private_data)
        )
        self.task.run(self.pk)


class TestInventoryUpdateCredentials(TestJobExecution):

    TASK_CLS = tasks.RunInventoryUpdate

    def get_instance(self):
        return InventoryUpdate(
            pk=1,
            inventory_source=InventorySource(
                pk=1,
                inventory=Inventory(pk=1)
            )
        )

    def test_ec2_source(self):
        aws = CredentialType.defaults['aws']()
        self.instance.source = 'ec2'
        self.instance.credential = Credential(
            credential_type=aws,
            inputs = {'username': 'bob', 'password': 'secret'}
        )
        self.instance.credential.inputs['password'] = encrypt_field(
            self.instance.credential, 'password'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args

            assert env['AWS_ACCESS_KEY_ID'] == 'bob'
            assert env['AWS_SECRET_ACCESS_KEY'] == 'secret'
            assert 'EC2_INI_PATH' in env

            config = ConfigParser.ConfigParser()
            config.read(env['EC2_INI_PATH'])
            assert 'ec2' in config.sections()
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_vmware_source(self):
        vmware = CredentialType.defaults['vmware']()
        self.instance.source = 'vmware'
        self.instance.credential = Credential(
            credential_type=vmware,
            inputs = {'username': 'bob', 'password': 'secret', 'host': 'https://example.org'}
        )
        self.instance.credential.inputs['password'] = encrypt_field(
            self.instance.credential, 'password'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args

            config = ConfigParser.ConfigParser()
            config.read(env['VMWARE_INI_PATH'])
            assert config.get('vmware', 'username') == 'bob'
            assert config.get('vmware', 'password') == 'secret'
            assert config.get('vmware', 'server') == 'https://example.org'
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_azure_source(self):
        azure = CredentialType.defaults['azure']()
        self.instance.source = 'azure'
        self.instance.credential = Credential(
            credential_type=azure,
            inputs = {
                'username': 'bob',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert env['AZURE_SUBSCRIPTION_ID'] == 'bob'
            ssh_key_data = env['AZURE_CERT_PATH']
            assert open(ssh_key_data, 'rb').read() == self.EXAMPLE_PRIVATE_KEY
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_gce_source(self):
        gce = CredentialType.defaults['gce']()
        self.instance.source = 'gce'
        self.instance.credential = Credential(
            credential_type=gce,
            inputs = {
                'username': 'bob',
                'project': 'some-project',
                'ssh_key_data': self.EXAMPLE_PRIVATE_KEY
            }
        )
        self.instance.credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            assert env['GCE_EMAIL'] == 'bob'
            assert env['GCE_PROJECT'] == 'some-project'
            ssh_key_data = env['GCE_PEM_FILE_PATH']
            assert open(ssh_key_data, 'rb').read() == self.EXAMPLE_PRIVATE_KEY
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_openstack_source(self):
        openstack = CredentialType.defaults['openstack']()
        self.instance.source = 'openstack'
        self.instance.credential = Credential(
            credential_type=openstack,
            inputs = {
                'username': 'bob',
                'password': 'secret',
                'project': 'tenant-name',
                'host': 'https://keystone.example.org'
            }
        )
        self.instance.credential.inputs['ssh_key_data'] = encrypt_field(
            self.instance.credential, 'ssh_key_data'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            shade_config = open(env['OS_CLIENT_CONFIG_FILE'], 'rb').read()
            assert '\n'.join([
                'clouds:',
                '  devstack:',
                '    auth:',
                '      auth_url: https://keystone.example.org',
                '      password: secret',
                '      project_name: tenant-name',
                '      username: bob',
                ''
            ]) in shade_config
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_satellite6_source(self):
        satellite6 = CredentialType.defaults['satellite6']()
        self.instance.source = 'satellite6'
        self.instance.credential = Credential(
            credential_type=satellite6,
            inputs = {
                'username': 'bob',
                'password': 'secret',
                'host': 'https://example.org'
            }
        )
        self.instance.credential.inputs['password'] = encrypt_field(
            self.instance.credential, 'password'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            config = ConfigParser.ConfigParser()
            config.read(env['FOREMAN_INI_PATH'])
            assert config.get('foreman', 'url') == 'https://example.org'
            assert config.get('foreman', 'user') == 'bob'
            assert config.get('foreman', 'password') == 'secret'
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)

    def test_cloudforms_source(self):
        cloudforms = CredentialType.defaults['cloudforms']()
        self.instance.source = 'cloudforms'
        self.instance.credential = Credential(
            credential_type=cloudforms,
            inputs = {
                'username': 'bob',
                'password': 'secret',
                'host': 'https://example.org'
            }
        )
        self.instance.credential.inputs['password'] = encrypt_field(
            self.instance.credential, 'password'
        )

        def run_pexpect_side_effect(*args, **kwargs):
            job, args, cwd, env, passwords, stdout = args
            config = ConfigParser.ConfigParser()
            config.read(env['CLOUDFORMS_INI_PATH'])
            assert config.get('cloudforms', 'url') == 'https://example.org'
            assert config.get('cloudforms', 'username') == 'bob'
            assert config.get('cloudforms', 'password') == 'secret'
            assert config.get('cloudforms', 'ssl_verify') == 'false'
            return ['successful', 0]

        self.task.run_pexpect = mock.Mock(side_effect=run_pexpect_side_effect)
        self.task.run(self.pk)


def test_os_open_oserror():
    with pytest.raises(OSError):
        os.open('this_file_does_not_exist', os.O_RDONLY)


def test_fcntl_ioerror():
    with pytest.raises(IOError):
        fcntl.flock(99999, fcntl.LOCK_EX)


@mock.patch('os.open')
@mock.patch('logging.getLogger')
def test_aquire_lock_open_fail_logged(logging_getLogger, os_open):
    err = OSError()
    err.errno = 3
    err.strerror = 'dummy message'

    instance = mock.Mock()
    instance.get_lock_file.return_value = 'this_file_does_not_exist'

    os_open.side_effect = err

    logger = mock.Mock()
    logging_getLogger.return_value = logger
    
    ProjectUpdate = tasks.RunProjectUpdate()

    with pytest.raises(OSError, errno=3, strerror='dummy message'):
        ProjectUpdate.acquire_lock(instance)
    assert logger.err.called_with("I/O error({0}) while trying to open lock file [{1}]: {2}".format(3, 'this_file_does_not_exist', 'dummy message'))


@mock.patch('os.open')
@mock.patch('os.close')
@mock.patch('logging.getLogger')
@mock.patch('fcntl.flock')
def test_aquire_lock_acquisition_fail_logged(fcntl_flock, logging_getLogger, os_close, os_open):
    err = IOError()
    err.errno = 3
    err.strerror = 'dummy message'

    instance = mock.Mock()
    instance.get_lock_file.return_value = 'this_file_does_not_exist'

    os_open.return_value = 3

    logger = mock.Mock()
    logging_getLogger.return_value = logger

    fcntl_flock.side_effect = err
    
    ProjectUpdate = tasks.RunProjectUpdate()

    with pytest.raises(IOError, errno=3, strerror='dummy message'):
        ProjectUpdate.acquire_lock(instance)
    os_close.assert_called_with(3)
    assert logger.err.called_with("I/O error({0}) while trying to aquire lock on file [{1}]: {2}".format(3, 'this_file_does_not_exist', 'dummy message'))
