import boto3
import subprocess
import requests
import paramiko
import time
import os
import sys

REGION = 'us-east-1'
AMI_ID = 'ami-0f3caa1cf4417e51b'
KEY_PATH = '/tmp/vockey.pem'
ECR_REGISTRY = os.environ['ECR_REGISTRY']
ECR_REPOSITORY = os.environ['ECR_REPOSITORY']

ENV_KEYS = [
    'DB_SERVER_HOST', 'DB_SERVER_USER', 'DB_SERVER_PASSWORD',
    'DB_SERVER_DATABASE', 'JWT_SECRET_KEY_ACCESS_TOKEN', 'JWT_SECRET_KEY_REFRESH_TOKEN',
]

ec2 = boto3.client('ec2', region_name=REGION)
instance_id = None


def wait_for_container(ssh, port, retries=12, delay=5):
    for i in range(retries):
        try:
            _, stdout, _ = ssh.exec_command(f'curl -s -o /dev/null -w "%{{http_code}}" http://localhost:{port}/api/products')
            code = stdout.read().decode().strip()
            if code == '200':
                print(f'Container ready after {i * delay}s')
                return
        except:
            pass
        print(f'Waiting... ({i+1}/{retries})')
        time.sleep(delay)
    raise Exception('Container never became ready')


def run(ssh, cmd):
    _, stdout, stderr = ssh.exec_command(cmd)
    if stdout.channel.recv_exit_status() != 0:
        raise Exception(f'Command failed: {stderr.read().decode()}')
    return stdout.read().decode()


try:
    with open(KEY_PATH, 'w') as f:
        f.write(os.environ['EC2_SSH_KEY'])
    os.chmod(KEY_PATH, 0o600)

    response = ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType='t3.micro',
        KeyName='vockey',
        SecurityGroupIds=[os.environ['QA_SECURITY_GROUP_ID']],
        UserData='#!/bin/bash',
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{
            'ResourceType': 'instance',
            'Tags': [{'Key': 'Name', 'Value': 'smoke-test-qa-ecommerce'}],
        }],
    )
    instance_id = response['Instances'][0]['InstanceId']
    print(f'Launched: {instance_id}')

    ec2.get_waiter('instance_status_ok').wait(InstanceIds=[instance_id])
    public_ip = ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['PublicIpAddress']
    print(f'IP: {public_ip}')

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(public_ip, username='ec2-user', key_filename=KEY_PATH)

    run(ssh, 'sudo yum install -y docker')
    run(ssh, 'sudo systemctl start docker')
    run(ssh, 'sudo usermod -a -G docker ec2-user')

    ecr_password = subprocess.check_output(['aws', 'ecr', 'get-login-password', '--region', REGION], text=True).strip()
    run(ssh, f'sudo docker login --username AWS --password {ecr_password} {ECR_REGISTRY}')
    run(ssh, f'sudo docker pull {ECR_REGISTRY}/{ECR_REPOSITORY}:test')

    env_flags = ' '.join(f'-e {k}={os.environ[k]}' for k in ENV_KEYS)
    run(ssh, f'sudo docker run -d --name ecommerce -p 3001:3001 {env_flags} {ECR_REGISTRY}/{ECR_REPOSITORY}:test')

    wait_for_container(ssh, 3001)

    base = f'http://{public_ip}:3001'
    print(f'Testing {base}')

    r = requests.get(f'{base}/api/products', timeout=10)
    print(f'GET /api/products -> {r.status_code}')
    assert r.status_code == 200, f'Products failed: {r.status_code}'

    r = requests.post(f'{base}/api/users/login',
                      json={'email': os.environ['SMOKE_TEST_EMAIL'], 'password': os.environ['SMOKE_TEST_PASSWORD']}, timeout=10)
    print(f'POST /api/users/login -> {r.status_code}')
    assert r.status_code in (200, 401), f'Login failed: {r.status_code}'

    print('Smoke tests passed!')

except Exception as e:
    print(f'Error: {e}')
    sys.exit(1)

finally:
    if instance_id:
        ec2.terminate_instances(InstanceIds=[instance_id])
    os.remove(KEY_PATH)
