from flask import Blueprint, abort, render_template, redirect, url_for
from translation_app.services.projects import get_project, list_languages, list_projects

pages = Blueprint('pages', __name__)


def project_view(project):
    names = {item['id']: item['name'] for item in list_languages(project['id'])}
    result = dict(project)
    result['languages'] = [{'id': language, 'name': names[language]} for language in project['languages']]
    return result


@pages.get('/')
def home():
    return redirect(url_for('pages.projects'))


@pages.get('/projects')
def projects():
    return render_template('projects.html', projects=list_projects())


def workspace_for(project_id):
    try:
        project = project_view(get_project(project_id))
    except ValueError:
        abort(404)
    if not project.get('enabled', True):
        abort(404)
    from translation_app.services.settings import model_settings
    models, default = model_settings()
    return render_template('workspace.html', project=project, model_options=models, default_model=default)


@pages.get('/direct')
def direct():
    return workspace_for('direct')


@pages.get('/projects/<project_id>')
def workspace(project_id):
    if project_id == 'direct':
        return redirect(url_for('pages.direct'))
    return workspace_for(project_id)
