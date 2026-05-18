Changelog format is based on [Keep a
Changelog](https://keepachangelog.com/en/1.0.0/).

Please respect the 80-character text margin and follow the [GitHub Flavored
Markdown Spec](https://github.github.com/gfm/).

### Security

### Fixed

### Added
- Notable new features in Django 5.2 LTS
  - `{% querystring %}` template tags:
    https://docs.djangoproject.com/en/5.2/releases/5.1/#querystring-template-tag
  - `query` and `fragment` arguments to `reverse()` and `reverse_lazy()`
    https://docs.djangoproject.com/en/5.2/releases/5.2/#urls

### Changed
- Update Dependency Roadmap
  - Base Docker image Ubuntu 22.04 LTS to 24.04 LTS
    - Python 3.10 to 3.12
    - Remove deprecated `version` key at root of Docker compose file
    - Update `requirements.lock` for Python 3.12, adds `setuptools` and `wheel`
  - MariaDB 10.6 to 11.4
  - Solr 9.x to 10.x
  - Django 4.2.x to 5.2.x

### Removed

### Migration
Review Django 5.0, 5.1, and 5.2 release notes:

- https://docs.djangoproject.com/en/5.2/releases/5.0/
  - Add setting `FORMS_URLFIELD_ASSUME_HTTPS = True` to transition to
    `forms.URLField` default scheme changing to `https` coming in Django 6.0
- https://docs.djangoproject.com/en/5.2/releases/5.1/
- https://docs.djangoproject.com/en/5.2/releases/5.2/
  - `django.template.context_processors.debug` no longer included in
    `django_defaults.py` settings file inside
    `TEMPLATES = [{..., 'OPTIONS': {'context_processors': [ HERE ,...]}]`
    - https://docs.djangoproject.com/en/5.2/ref/templates/api/#django.template.context_processors.debug
    - Removes the ability to check if app in `DEBUG` mode and `sql_queries`
      timing in templates

Review dependencies and any local customizations for breaking changes:
- Solr 9.x to 10.x: https://solr.apache.org/guide/solr/latest/upgrade-notes/major-changes-in-solr-10.html
  - Note a full re-index is encouraged for making one major version upgrade,
    and **required** if upgrading two or more major versions

### Deprecated

### Contributors
- Greg Tunink (techgique)
