import boto3
import subprocess
import requests
import paramiko
import shlex
import time
import os
import sys

REGION = 'us-east-1'
KEY_PATH = '/tmp/vockey.pem'

ENV_KEYS = [
    'DB_SERVER_HOST', 'DB_SERVER_USER', 'DB_SERVER_PASSWORD',
    'DB_SERVER_DATABASE', 'JWT_SECRET_KEY_ACCESS_TOKEN', 'JWT_SECRET_KEY_REFRESH_TOKEN',
]

ec2 = boto3.client('ec2', region_name=REGION)
instance_id = None


def run(client, cmd):
    _, stdout, stderr = client.exec_command(cmd)
    if stdout.channel.recv_exit_status() != 0:
        raise Exception(f'SSH failed: {stderr.read().decode()}')
    return stdout.read().decode()


try:
    ami_id = boto3.client('ssm', region_name=REGION).get_parameter(
        Name='/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64'
    )['Parameter']['Value']

    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType='t3.micro',
        KeyName='vockey',
        SecurityGroupIds=[os.environ['QA_SECURITY_GROUP_ID']],
        UserData='#!/bin/bash\nyum install -y docker\nsystemctl start docker\nusermod -a -G docker ec2-user',
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

    with open(KEY_PATH, 'w') as f:
        f.write(os.environ['EC2_SSH_KEY'])
    os.chmod(KEY_PATH, 0o600)

    subprocess.run(
        f'docker save ecommerce-app:test | gzip | ssh -o StrictHostKeyChecking=no -i {KEY_PATH} ec2-user@{public_ip} "gunzip | docker load"',
        shell=True, check=True
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(public_ip, username='ec2-user', key_filename=KEY_PATH)

    env_flags = ' '.join(f'-e {k}={shlex.quote(os.environ[k])}' for k in ENV_KEYS)
    run(client, f'docker run -d --name ecommerce -p 3001:3001 {env_flags} ecommerce-app:test node /app/app.js')

    time.sleep(10)

    base = f'http://{public_ip}:3001'
    print(f'Testing {base}')

    r = requests.get(f'{base}/api/products', timeout=10)
    print(f'GET /api/products -> {r.status_code}')
    assert r.status_code == 200, f'Products failed: {r.status_code}'

    r = requests.post(f'{base}/api/users/login',
                      json={'email': 'c@gmail.com', 'password': '123456'}, timeout=10)
    print(f'POST /api/users/login -> {r.status_code}')
    assert r.status_code in (200, 401), f'Login failed: {r.status_code}'

    print('Smoke tests passed!')

except Exception as e:
    print(f'Error: {e}')
    sys.exit(1)

finally:
    if instance_id:
        ec2.terminate_instances(InstanceIds=[instance_id])
    subprocess.run(['rm', '-f', KEY_PATH])
