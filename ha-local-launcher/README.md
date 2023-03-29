# RStudio Workbench High Availability and Local Launcher

![](infra.drawio.png)

## Usage

Before getting started please read the project [README](../../README.md) to ensure you have all of the required dependencies installed.

There are three primary files / directories:

- `__main__.py`: contains the python code that will stand up the AWS resources.
- `server-side-files/justfile`: contains the commands required to install RSW and the required dependencies. This file will be copied to each ec2 instance so that it can be executed on the server.

### Step 1: Log into AWS

```bash
aws sso login
```

### Step 2: Create new virtual environment

```bash
python -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
```

### Step 3: Pulumi configuration

Select your pulumi stack.

```bash
pulumi stack select dev
```

Create a new key pair to be used with AWS:

```
just key-pair-new
```

Set the following pulumi configuration values:

```bash
pulumi config set email <XXXX>
pulumi config set --secret rsw_license $RSW_LICENSE
cat key.pub | pulumi config set public_key
```

### Step 4: Spin up infra

Create all of the infrastructure.

```bash
pulumi up
```

### Step 5: Validate that RSW is working

Visit RSW in your browser:

```bash
just server-open
```

Start a few new sessions. Verify that the sessions are being balanced across the servers.

```bash
just server-load-status
```
