# Open ONI Ubuntu Installation

Open ONI currently favors of using [Docker configuration](/docs/install/docker.md) over manual instructions.

The current version of Ubuntu that is used in the docker configuration is Ubuntu 18.04 (codenamed Bionic Beaver), 
so that is the version this documentation will use. At the time of this writting (October 2020) the python virtual
environment doesn't work properly on windows subsystem for linux (WSL). It is recommended to run this on a bare
metal server or a dedicated virtual machine.

NOTE: This install will be using the default user(using the environment variable $USER). If you are running this in production change to whatever user 
that is associated with openoni that you have created. In addition, change permissions as needed for security
(A good place to start would be 755).

# Update and install dependencies
We first need to update and install dependencies by running

```bash
sudo apt-get update
sudo apt-get -y install --no-install-recommends apache2 ca-certificates gcc git libmysqlclient-dev libssl-dev libxml2-dev libxslt-dev libjpeg-dev mysql-client curl rsync python3-dev python3-pip libapache2-mod-wsgi-py3
```

# Force apache error logs to stderr
run
```bash
sudo ln -sf /proc/self/fd/1 /var/log/apache2/error.log
```

# Enable and configure apache modules
run
```bash
sudo a2enmod cache cache_disk expires rewrite proxy_http ssl
sudo service apache2 restart
sudo service apache-htcacheclean start
sudo mkdir -p /var/cache/httpd/mod_disk_cache
sudo chown -R www-data:www-data /var/cache/httpd
sudo a2dissite 000-default.conf
sudo service apache2 reload
```

# Replace shell with bash
run
```bash
sudo rm /bin/sh
sudo ln -s /bin/bash /bin/sh
```

# Checkout open-oni code
run
```bash
sudo git clone https://github.com/open-oni/open-oni.git /opt/openoni
sudo chown -R $USER:$USER /opt/openoni
```

# Add cron to clean out cache
run
```bash
echo "/usr/local/bin/manage delete_cache" | sudo tee -a /etc/cron.daily/delete_cache
sudo chmod u+x /etc/cron.daily/delete_cache
```

# Verify config
run
```bash
cd /opt/openoni

# Make sure settings_local.py exists so the app doesn't crash
if [ ! -f ./settings_local.py ]; then
	cp ./settings_local_example.py ./settings_local.py
fi
# Make sure we have a default urls.py
if [ ! -f ./urls.py ]; then
	cp ./urls_example.py ./urls.py
fi

# Prepare the ENV dir if necessary
# Create and activate Python virtual environment
pip3 install -U pip
pip install -U setuptools
pip install -U virtualenv
virtualenv -p python3 ENV
source ENV/bin/activate

# Install / update Open ONI dependencies
pip install -U -r requirements.pip

# Miscellaneous
install -d /opt/openoni/static
install -d /opt/openoni/.python-eggs

# Update requirements.lock
pip list --format freeze > requirements.lock
```

# Setup database
run
```bash
# Setup a test database
mysql -uroot -hrdbms -p123456 -e 'USE mysql; GRANT ALL on test_openoni.* TO "openoni"@"%" IDENTIFIED BY "openoni";'

/opt/openoni/manage.py migrate
```

# Setup index
run
```bash
# Installing Solr configs
/opt/openoni/manage.py setup_index
```

# Prep webserver
run
```bash
sudo mkdir -p /var/tmp/django_cache && chown -R www-data:www-data /var/tmp/django_cache
mkdir -p /opt/openoni/log

# Update Apache config
sudo cp /opt/openoni/docker/apache/openoni.conf /etc/apache2/sites-available/openoni.conf

# Set Apache log level from APACHE_LOG_LEVEL in .env file
sudo sed -i "s/!LOGLEVEL!/$APACHE_LOG_LEVEL/g" /etc/apache2/sites-available/openoni.conf

# Enable updated Apache config
sudo a2ensite openoni

# Get static files ready for Apache to serve
# Django needs write access to STATIC_ROOT and the log directory
sudo chown -R www-data:www-data /opt/openoni/static/compiled
sudo chown -R www-data:www-data /opt/openoni/log
/opt/openoni/manage.py collectstatic --noinput
sudo rm -f /var/run/apache2/apache2.pid
```

# Run apache
run
```bash
apache2 -D FOREGROUND
```