# set up basic requirements for MobileClaw
from setuptools import setup, find_packages
import glob
import os

setup(
    name='nationclaw',
    packages=find_packages(include=['nationclaw', 'nationclaw.*']),
    # this must be the same as the name above
    version='0.3.1',
    description='Development framework and runtime system for mobile agents.',
    author='MobileClaw Team',
    license='MIT',
    author_email='li.yuanchun@foxmail.com',
    url='https://github.com/opeopanation-lab/NationClaw',  # use the URL to the github repo
    download_url='https://github.com/opeopanation-lab/NationClaw/tarball/0.3.1',
    keywords=['AI', 'agent', 'mobile', 'framework', 'LLM'],  # arbitrary keywords
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 3 - Alpha',

        # Indicate who your project is intended for
        'Intended Audience :: Developers',
        'Topic :: Software Development',

        # Specify the Python versions you support here. In particular, ensure
        # that you indicate whether you support Python 2, Python 3 or both.
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
    python_requires='>=3.10',
    entry_points={
        'console_scripts': [
            'nationclaw=nationclaw.main:main',
            'nationclaw-gateway=nationclaw.gateway.app:main',
        ],
    },
    package_data={
        'nationclaw': [os.path.relpath(x, 'nationclaw') for x in glob.glob('nationclaw/resources/**/*', recursive=True)]
    },
    install_requires=[
        'websocket-client',
        'websockets',
        "structlog",
        "markitdown",
        'pyyaml',
        "Pillow",
        "cryptography",
        'requests',
        'numpy',
        'zulip',
        'lark_oapi',
        'qq-botpy>=1.2.0',
        'python-telegram-bot>=20.0',
        'slack_sdk',
        'fastapi',
        'uvicorn',
    ],
)
