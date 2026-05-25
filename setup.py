from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="msuite",
    version="1.0.0",
    description="SaaS subscription and entitlement management",
    author="MSuite",
    author_email="dev@msuite.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
