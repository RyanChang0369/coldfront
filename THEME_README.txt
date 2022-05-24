Theme only takes effect if you pass the environment variables SITE_STATIC and
SITE_TEMPLATES as shown below. 

    SITE_STATIC = "${workspaceFolder}/coldfront/site/static",
    SITE_TEMPLATES = "${workspaceFolder}/coldfront/site/templates"

Where ${workspaceFolder} is the installation directory of Coldfront. See
https://coldfront.readthedocs.io/en/latest/config/#custom-branding.

Note that the gitignore file was altered to allow for the theme to be exported
here.