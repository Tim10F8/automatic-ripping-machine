#!/usr/bin/env python3
"""Main routes for the A.R.M ui"""
import os
import re
import sys  # noqa: F401
import hashlib
import json
from pathlib import Path, PurePath

import requests
import bcrypt
import psutil
from werkzeug.exceptions import HTTPException
from werkzeug.routing import ValidationError
from flask import Flask, render_template, request, send_file, flash, \
    redirect, url_for  # noqa: F401
from flask.logging import default_handler  # noqa: F401
from flask_login import LoginManager, login_required, \
    current_user, login_user, UserMixin, logout_user  # noqa: F401
import arm.ui.utils as ui_utils
from arm.ui import app, db, constants, json_api
from arm.models import models as models
from arm.config.config import cfg
from arm.ui.forms import TitleSearchForm, ChangeParamsForm, SettingsForm, UiSettingsForm, SetupForm
from arm.ui.metadata import get_omdb_poster

login_manager = LoginManager()
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    """
    Logged in check
    :param user_id:
    :return:
    """
    try:
        return models.User.query.get(int(user_id))
    except Exception:
        app.logger.debug("Error getting user")
        return None


@login_manager.unauthorized_handler
def unauthorized():
    """
    User isn't authorised to view the page
    :return: Page redirect
    """
    return redirect('/login')


@app.route('/error')
def was_error(error):
    """
    Catch all error page
    :return: Error page
    """
    return render_template(constants.ERROR_PAGE, title='error', error=error)


@app.route("/logout")
def logout():
    """
    Log user out
    :return:
    """
    logout_user()
    flash("logged out", "success")
    return redirect('/')


@app.route('/setup')
def setup():
    """
    This is the initial setup page for fresh installs
    This is no longer recommended for upgrades

    This function will do various checks to make sure everything can be setup for ARM
    Directory ups, create the db, etc
    """
    perm_file = Path(PurePath(cfg['INSTALLPATH'], "installed"))
    app.logger.debug("perm " + str(perm_file))
    # Check for install file and that db is correctly setup
    if perm_file.exists() and ui_utils.setup_database():
        flash(str(perm_file) + " exists, setup cannot continue."
                               " To re-install please delete this file.", "danger")
        return redirect("/")
    dir0 = Path(PurePath(cfg['DBFILE']).parent)
    dir1 = Path(cfg['RAW_PATH'])
    dir2 = Path(cfg['TRANSCODE_PATH'])
    dir3 = Path(cfg['COMPLETED_PATH'])
    dir4 = Path(cfg['LOGPATH'])
    arm_directories = [dir0, dir1, dir2, dir3, dir4]

    try:
        for arm_dir in arm_directories:
            if not Path.exists(arm_dir):
                os.makedirs(arm_dir)
                flash(f"{arm_dir} was created successfully.", "success")
    except FileNotFoundError as error:
        flash(f"Creation of the directory {dir0} failed {error}", "danger")
        app.logger.debug(f"Creation of the directory failed - {error}")
    else:
        flash("Successfully created all of the ARM directories", "success")
        app.logger.debug("Successfully created all of the ARM directories")

    try:
        if ui_utils.setup_database():
            flash("Setup of the database was successful.", "success")
            app.logger.debug("Setup of the database was successful.")
            perm_file = Path(PurePath(cfg['INSTALLPATH'], "installed"))
            write_permission_file = open(perm_file, "w")
            write_permission_file.write("boop!")
            write_permission_file.close()
            return redirect(constants.HOME_PAGE)
        flash("Couldn't setup database", "danger")
        app.logger.debug("Couldn't setup database")
        return redirect("/error")
    except Exception as error:
        flash(str(error))
        app.logger.debug("Setup - " + str(error))
        return redirect(constants.HOME_PAGE)


@app.route('/update_password', methods=['GET', 'POST'])
@login_required
def update_password():
    """
    updating password for the admin account
    """
    # After a login for is submitted
    form = SetupForm()
    if form.validate_on_submit():
        username = str(request.form['username']).strip()
        new_password = str(request.form['newpassword']).strip().encode('utf-8')
        user = models.User.query.filter_by(email=username).first()
        password = user.password
        hashed = user.hash
        # our new one
        login_hashed = bcrypt.hashpw(str(request.form['password']).strip().encode('utf-8'), hashed)
        if login_hashed == password:
            hashed_password = bcrypt.hashpw(new_password, hashed)
            user.password = hashed_password
            user.hash = hashed
            try:
                db.session.commit()
                flash("Password successfully updated", "success")
                return redirect("logout")
            except Exception as error:
                flash(str(error), "danger")
        else:
            flash("Password couldn't be updated. Problem with old password", "danger")
    return render_template('update_password.html', form=form)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """
    Login page if login is enabled
    :return: redirect
    """
    return_redirect = None
    # if there is no user in the database
    try:
        user_list = models.User.query.all()
        # If we don't raise an exception but the usr table is empty
        if not user_list:
            app.logger.debug("No admin found")
            return_redirect = redirect(constants.SETUP_STAGE_2)
    except Exception:
        flash(constants.NO_ADMIN_ACCOUNT, "danger")
        app.logger.debug(constants.NO_ADMIN_ACCOUNT)
        return_redirect = redirect(constants.SETUP_STAGE_2)

    # if user is logged in
    if current_user.is_authenticated:
        return_redirect = redirect(constants.HOME_PAGE)

    form = SetupForm()
    if form.validate_on_submit():
        login_username = request.form['username']
        # we know there is only ever 1 admin account, so we can pull it and check against it locally
        admin = models.User.query.filter_by().first()
        app.logger.debug("user= " + str(admin))
        # our pass
        password = admin.password
        # hashed pass the user provided
        login_hashed = bcrypt.hashpw(str(request.form['password']).strip().encode('utf-8'), admin.hash)

        if login_hashed == password and login_username == admin.email:
            login_user(admin)
            app.logger.debug("user was logged in - redirecting")
            return_redirect = redirect(constants.HOME_PAGE)
        else:
            flash("Something isn't right", "danger")
    # If nothing has gone wrong give them the login page
    if return_redirect is None:
        return_redirect = render_template('login.html', form=form)
    return return_redirect


@app.route('/database')
@login_required
def database():
    """
    The main database page

    Outputs every job from the database
     this can cause serious slow-downs with + 3/4000 entries
    """

    page = request.args.get('page', 1, type=int)
    # Check for database file
    if os.path.isfile(cfg['DBFILE']):
        jobs = models.Job.query.order_by(db.desc(models.Job.job_id)).paginate(page, 100, False)
    else:
        app.logger.error('ERROR: /database no database, file doesnt exist')
        jobs = {}
    return render_template('database.html', jobs=jobs.items,
                           date_format=cfg['DATE_FORMAT'], pages=jobs)


@app.route('/json', methods=['GET', 'POST'])
@login_required
def feed_json():
    """
    json mini API
    This is used for all api/ajax calls this makes thing easier to read/code for
    Adding a new function to the api is as simple as adding a new elif where GET[mode]
    is your call
    You can then add a function inside utils to deal with the request
    """
    return_json = {}
    mode = request.args.get('mode')
    j_id = request.args.get('job')
    searchq = request.args.get('q')
    logpath = cfg['LOGPATH']

    if mode == "delete":
        return_json = json_api.delete_job(j_id, mode)
    elif mode == "abandon":
        return_json = json_api.abandon_job(j_id)
    elif mode == "full":
        app.logger.debug("getlog")
        return_json = json_api.generate_log(logpath, j_id)
    elif mode == "search":
        app.logger.debug("search")
        return_json = json_api.search(searchq)
    elif mode == "getfailed":
        app.logger.debug("getfailed")
        return_json = json_api.get_x_jobs("fail")
    elif mode == "getsuccessful":
        app.logger.debug("getsucessful")
        return_json = json_api.get_x_jobs("success")
    elif mode == "joblist":
        app.logger.debug("joblist")
        return_json = json_api.get_x_jobs("joblist")
    elif mode == "fixperms":
        app.logger.debug("fixperms")
        return_json = ui_utils.fix_permissions(j_id)
    elif mode == "send_item":
        app.logger.debug("send_item")
        return_json = ui_utils.send_to_remote_db(j_id)
    app.logger.debug(return_json)
    return app.response_class(response=json.dumps(return_json, indent=4, sort_keys=True),
                              status=200,
                              mimetype=constants.JSON_TYPE)


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """
    The settings page - allows the user to update the arm.yaml without needing to open a text editor
    Also triggers a restart of flask for debugging.

    This needs to be rewritten to be static
    """
    arm_cfg_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../..", "arm.yaml")
    comments = ui_utils.generate_comments()
    current_cfg = ui_utils.get_settings(arm_cfg_file)

    form = SettingsForm()
    if form.validate_on_submit():
        # Build the new arm.yaml with updated values from the user
        arm_cfg = ui_utils.save_settings(request.form.to_dict(), comments)
        # Save updated arm.yaml
        with open(arm_cfg_file, "w") as settings_file:
            settings_file.write(arm_cfg)
            settings_file.close()
        flash("Setting saved successfully!", "success")
        return redirect(url_for('settings'))
    # If we get to here there was no post data
    return render_template('settings.html', settings=current_cfg,
                           form=form, raw=request.form.to_dict(), jsoncomments=comments)


@app.route('/ui_settings', methods=['GET', 'POST'])
@login_required
def ui_settings():
    """
    The ARMui settings page - allows the user to update the armui_settings
    This function needs to trigger a restart of flask for debugging to update the values

    """
    armui_cfg = models.UISettings.query.filter_by().first()
    form = UiSettingsForm()
    if form.validate_on_submit():
        # json.loads("false".lower())
        use_icons = (str(form.use_icons.data).strip().lower() == "true")
        save_remote_images = (str(form.save_remote_images.data).strip().lower() == "true")
        database_arguments = {
            'index_refresh': format(form.index_refresh.data),
            'use_icons': use_icons,
            'save_remote_images': save_remote_images,
            'bootstrap_skin': format(form.bootstrap_skin.data),
            'language': format(form.language.data),
            'database_limit': format(form.database_limit.data),
        }
        ui_utils.database_updater(database_arguments, armui_cfg)
        db.session.refresh(armui_cfg)
        flash("Settings saved successfully!", "success")

    return render_template('ui_settings.html', form=form, settings=armui_cfg)


@app.route('/logs')
@login_required
def logs():
    """
    This is the main page for viewing a logfile

    this holds the XHR request that sends to other routes for the data
    """
    mode = request.args['mode']
    logfile = request.args['logfile']

    return render_template('logview.html', file=logfile, mode=mode)


@app.route('/listlogs', defaults={'path': ''})
@login_required
def listlogs(path):
    """
    The 'View logs' page - show a list of logfiles in the log folder with creation time and size
    Gives the user links to tail/arm/Full/download
    """
    base_path = cfg['LOGPATH']
    full_path = os.path.join(base_path, path)

    # Deal with bad data
    if not os.path.exists(full_path):
        raise ValidationError

    # Get all files in directory
    files = ui_utils.get_info(full_path)
    return render_template('logfiles.html', files=files, date_format=cfg['DATE_FORMAT'])


@app.route('/logreader')
@login_required
def logreader():
    """
    The default logreader output function

    This will display or allow downloading the requested logfile
    This is where the XHR requests are sent when viewing /logs?=logfile
    """
    log_path = cfg['LOGPATH']
    mode = request.args.get('mode')
    # We should use the job id and not get the raw logfile from the user
    # TODO poss search database and see if we can match the logname with a previous rip ?
    full_path = os.path.join(log_path, request.args.get('logfile'))
    ui_utils.validate_logfile(request.args.get('logfile'), mode, Path(full_path))

    # Only ARM logs
    if mode == "armcat":
        generate = ui_utils.generate_arm_cat(full_path)
    # Give everything / Tail
    elif mode == "full":
        generate = ui_utils.generate_full_log(full_path)
    elif mode == "download":
        return send_file(full_path, as_attachment=True)
    else:
        # No mode - error out
        raise ValidationError

    return app.response_class(generate, mimetype='text/plain')


@app.route('/activerips')
@login_required
def rips():
    """
    This no longer works properly because of the 'transcoding' status
    """
    return render_template('activerips.html', jobs=models.Job.query.filter_by(status="active"))


@app.route('/history')
@login_required
def history():
    """
    Smaller much simpler output of previously run jobs

    """
    page = request.args.get('page', 1, type=int)
    if os.path.isfile(cfg['DBFILE']):
        # after roughly 175 entries firefox readermode will break
        # jobs = Job.query.filter_by().limit(175).all()
        jobs = models.Job.query.order_by(db.desc(models.Job.job_id)).paginate(page, 100, False)
    else:
        app.logger.error('ERROR: /history database file doesnt exist')
        jobs = {}
    app.logger.debug(f"Date format - {cfg['DATE_FORMAT']}")

    return render_template('history.html', jobs=jobs.items,
                           date_format=cfg['DATE_FORMAT'], pages=jobs)


@app.route('/jobdetail')
@login_required
def jobdetail():
    """
    Page for showing in-depth details about a job

    Shows Job/Config/Track class details
    displays them in a clear and easy to ready format
    """
    job_id = request.args.get('job_id')
    job = models.Job.query.get(job_id)
    tracks = job.tracks.all()
    search_results = ui_utils.metadata_selector("get_details", job.title, job.year, job.imdb_id)
    if search_results and 'Error' not in search_results:
        job.plot = search_results['Plot'] if 'Plot' in search_results else "There was a problem getting the plot"
        job.background = search_results['background_url'] if 'background_url' in search_results else None
    return render_template('jobdetail.html', jobs=job, tracks=tracks, s=search_results)


@app.route('/titlesearch', methods=['GET', 'POST'])
@login_required
def submitrip():
    """
    The initial search page
    """
    job_id = request.args.get('job_id')
    job = models.Job.query.get(job_id)
    form = TitleSearchForm(obj=job)
    if form.validate_on_submit():
        form.populate_obj(job)
        flash(f'Search for {form.title.data}, year={form.year.data}', 'success')
        return redirect(url_for('list_titles', title=form.title.data, year=form.year.data, job_id=job_id))
    return render_template('titlesearch.html', title='Update Title', form=form, job=job)


@app.route('/changeparams', methods=['GET', 'POST'])
@login_required
def changeparams():
    """
    For updating Config params or changing/correcting job.disctype manually
    """
    config_id = request.args.get('config_id')
    # app.logger.debug(config.pretty_table())
    job = models.Job.query.get(config_id)
    config = job.config
    form = ChangeParamsForm(obj=config)
    if form.validate_on_submit():
        job.disctype = format(form.DISCTYPE.data)
        cfg["MINLENGTH"] = config.MINLENGTH = format(form.MINLENGTH.data)
        cfg["MAXLENGTH"] = config.MAXLENGTH = format(form.MAXLENGTH.data)
        cfg["RIPMETHOD"] = config.RIPMETHOD = format(form.RIPMETHOD.data)
        # must be 1 for True 0 for False
        cfg["MAINFEATURE"] = config.MAINFEATURE = 1 if format(form.MAINFEATURE.data).lower() == "true" else 0
        args = {'disctype': job.disctype}
        # We don't need to set the config as they are set with job commit
        ui_utils.database_updater(args, job)

        flash(f'Parameters changed. Rip Method={config.RIPMETHOD}, Main Feature={config.MAINFEATURE},'
              f'Minimum Length={config.MINLENGTH}, '
              f'Maximum Length={config.MAXLENGTH}, Disctype={job.disctype}', "success")
    return render_template('changeparams.html', title='Change Parameters', form=form)


@app.route('/customTitle', methods=['GET', 'POST'])
@login_required
def customtitle():
    """
    For setting custom title for series with multiple discs
    """
    job_id = request.args.get('job_id')
    ui_utils.job_id_validator(job_id)
    job = models.Job.query.get(job_id)
    form = TitleSearchForm(obj=job)
    if form.validate_on_submit():
        form.populate_obj(job)
        job.title = format(form.title.data)
        job.year = format(form.year.data)
        args = {
            'title': job.disctype,
            'year': job.year
        }
        ui_utils.database_updater(args, job)
        flash(f'custom title changed. Title={form.title.data}, Year={form.year.data}.', "success")
        return redirect(url_for('home'))
    return render_template('customTitle.html', title='Change Title', form=form, job=job)


@app.route('/list_titles', methods=['GET'])
@login_required
def list_titles():
    """
    The search results page

    This will display the returned search results from OMDB or TMDB from the users input search
    """
    title = request.args.get('title').strip() if request.args.get('title') else ''
    year = request.args.get('year').strip() if request.args.get('year') else ''
    job_id = request.args.get('job_id').strip() if request.args.get('job_id') else ''
    if job_id == "":
        app.logger.debug("list_titles - no job supplied")
        flash(constants.NO_JOB, "danger")
        raise ValidationError
    job = models.Job.query.get(job_id)
    form = TitleSearchForm(obj=job)
    search_results = ui_utils.metadata_selector("search", title, year)
    if search_results is None or 'Error' in search_results or (
            'Search' in search_results and len(search_results['Search']) < 1):
        app.logger.debug("No results found. Trying without year")
        flash(f"No search results found for {title} ({year})<br/> Trying without year", 'danger')
        search_results = ui_utils.metadata_selector("search", title, "")

    if search_results is None or 'Error' in search_results or (
            'Search' in search_results and len(search_results['Search']) < 1):
        flash(f"No search results found for {title}", 'danger')
    return render_template('list_titles.html', results=search_results, job_id=job_id,
                           form=form, title=title, year=year)


@app.route('/gettitle', methods=['GET'])
@app.route('/select_title', methods=['GET'])
@login_required
def gettitle():
    """
    Used to display plot info from the search result page when the user clicks the title
    and to forward the user to save the selected details

    This was also used previously for the getdetails page but it no longer needed there
    """
    imdb_id = request.args.get('imdbID').strip() if request.args.get('imdbID') else None
    job_id = request.args.get('job_id').strip() if request.args.get('job_id') else None
    if imdb_id == "" or imdb_id is None:
        app.logger.debug("gettitle - no imdb supplied")
        flash("No imdb supplied", "danger")
        raise ValidationError("No imdb supplied")
    if job_id == "" or job_id is None:
        app.logger.debug("gettitle - no job supplied")
        flash(constants.NO_JOB, "danger")
        raise ValidationError(constants.NO_JOB)
    dvd_info = ui_utils.metadata_selector("get_details", None, None, imdb_id)
    return render_template('showtitle.html', results=dvd_info, job_id=job_id)


@app.route('/updatetitle', methods=['GET'])
@login_required
def updatetitle():
    """
    used to save the details from the search
    """
    # updatetitle?title=Home&amp;year=2015&amp;imdbID=tt2224026&amp;type=movie&amp;
    #  poster=http://image.tmdb.org/t/p/original/usFenYnk6mr8C62dB1MoAfSWMGR.jpg&amp;job_id=109
    new_title = request.args.get('title')
    new_year = request.args.get('year')
    video_type = request.args.get('type')
    imdb_id = request.args.get('imdbID')
    poster_url = request.args.get('poster')
    job_id = request.args.get('job_id')
    app.logger.debug("New imdbID=" + str(imdb_id))
    job = models.Job.query.get(job_id)
    job.title = ui_utils.clean_for_filename(new_title)
    job.title_manual = ui_utils.clean_for_filename(new_title)
    job.year = new_year
    job.year_manual = new_year
    job.video_type_manual = video_type
    job.video_type = video_type
    job.imdb_id_manual = imdb_id
    job.imdb_id = imdb_id
    job.poster_url_manual = poster_url
    job.poster_url = poster_url
    job.hasnicetitle = True
    db.session.commit()
    flash(f'Title: {job.title_auto} ({job.year_auto}) was updated to {new_title} ({new_year})', "success")
    return redirect("/")


@app.route('/')
@app.route('/index.html')
@app.route('/index')
@login_required
def home():
    """
    The main homepage showing current rips and server stats
    """
    # Force a db update
    ui_utils.check_db_version(cfg['INSTALLPATH'], cfg['DBFILE'])

    # Hard drive space
    try:
        freegb = psutil.disk_usage(cfg['TRANSCODE_PATH']).free
        freegb = round(freegb / 1073741824, 1)
        arm_percent = psutil.disk_usage(cfg['TRANSCODE_PATH']).percent
        mfreegb = psutil.disk_usage(cfg['COMPLETED_PATH']).free
        mfreegb = round(mfreegb / 1073741824, 1)
        media_percent = psutil.disk_usage(cfg['COMPLETED_PATH']).percent
    except FileNotFoundError:
        freegb = 0
        arm_percent = 0
        mfreegb = 0
        media_percent = 0
        app.logger.debug("ARM folders not found")
        flash("There was a problem accessing the ARM folders. Please make sure you have setup ARM<br/>"
              "Setup can be started by visiting <a href=\"/setup\">setup page</a> ARM will not work correctly until"
              "until you have added an admin account", "danger")
    # We could check for the install file here  and then error out if we want
    #  RAM
    memory = psutil.virtual_memory()
    mem_total = round(memory.total / 1073741824, 1)
    mem_free = round(memory.available / 1073741824, 1)
    mem_used = round(memory.used / 1073741824, 1)
    ram_percent = memory.percent

    armname = ""
    if cfg['ARM_NAME'] != "":
        armname = f"[{cfg['ARM_NAME']}] - "

    #  get out cpu info
    try:
        our_cpu = ui_utils.get_processor_name()
        cpu_usage = psutil.cpu_percent()
    except EnvironmentError:
        our_cpu = "Not found"
        cpu_usage = "0"

    try:
        temps = psutil.sensors_temperatures()
        temp = temps['coretemp'][0][1]
    except KeyError:
        temp = temps = None

    if os.path.isfile(cfg['DBFILE']):
        try:
            jobs = db.session.query(models.Job).filter(models.Job.status.notin_(['fail', 'success'])).all()
        except Exception:
            # db isn't setup
            return redirect(url_for('setup'))
    else:
        jobs = {}

    return render_template('index.html', freegb=freegb, mfreegb=mfreegb,
                           arm_percent=arm_percent, media_percent=media_percent,
                           jobs=jobs, cpu=our_cpu, cputemp=temp, cpu_usage=cpu_usage,
                           ram=mem_total, ramused=mem_used, ramfree=mem_free, ram_percent=ram_percent,
                           ramdump=str(temps), armname=armname, children=cfg['ARM_CHILDREN'])


@app.route('/import_movies')
@login_required
def import_movies():
    """
    Function for finding all movies not currently tracked by ARM in the MEDIA_DIR
    This should not be run frequently
    This causes a HUGE number of requests to OMdb
    :return: Outputs json - contains a dict/json of movies added and a notfound list
             that doesnt match ARM identified folder format.
    """
    import time
    from os import listdir
    from os.path import isfile, join, isdir
    time_0 = time.time()

    my_path = cfg['COMPLETED_PATH']
    movies = {0: {'notfound': {}}}
    dest_ext = cfg['DEST_EXT']
    i = 1
    movie_dirs = [f for f in listdir(my_path) if isfile(join(my_path, f)) and not f.startswith(".")
                  or isdir(join(my_path, f)) and not f.startswith(".")]

    app.logger.debug(movie_dirs)
    if len(movie_dirs) < 1:
        app.logger.debug("movie_dirs found none")

    for movie in movie_dirs:
        mystring = f"{movie}"
        regex = r"([\w\ \'\.\-\&\,]*?) \(([0-9]{2,4})\)"
        matched = re.match(regex, movie)
        if matched:
            # This is only for pycharm
            movie_name = str.replace(" ", "%20", matched.group(1).strip())  # movie

            p1, imdb_id = get_omdb_poster(movie_name, matched.group(2))
            # ['poster.jpg', 'title_t00.mkv', 'title_t00.xml', 'fanart.jpg',
            #  'title_t00.nfo-orig', 'title_t00.nfo', 'title_t00.xml-orig', 'folder.jpg']
            app.logger.debug(str(listdir(join(my_path, str(movie)))))
            movie_files = [f for f in listdir(join(my_path, str(movie)))
                           if isfile(join(my_path, str(movie), f)) and f.endswith("." + dest_ext)
                           or isfile(join(my_path, str(movie), f)) and f.endswith(".mp4")
                           or isfile(join(my_path, str(movie), f)) and f.endswith(".avi")]
            app.logger.debug("movie files = " + str(movie_files))

            hash_object = hashlib.md5(mystring.encode())
            dupe_found, not_used_variable = ui_utils.job_dupe_check(hash_object.hexdigest())
            if dupe_found:
                app.logger.debug("We found dupes breaking loop")
                continue

            movies[i] = {
                'title': matched.group(1),
                'year': matched.group(2),
                'crc_id': hash_object.hexdigest(),
                'imdb_id': imdb_id,
                'poster': p1,
                'status': 'success' if len(movie_files) > 0 else 'fail',
                'video_type': 'movie',
                'disctype': 'unknown',
                'hasnicetitle': True,
                'no_of_titles': len(movie_files)
            }

            new_movie = models.Job("/dev/sr0")
            new_movie.title = movies[i]['title']
            new_movie.year = movies[i]['year']
            new_movie.crc_id = hash_object.hexdigest()
            new_movie.imdb_id = imdb_id
            new_movie.poster_url = movies[i]['poster']
            new_movie.status = movies[i]['status']
            new_movie.video_type = movies[i]['video_type']
            new_movie.disctype = movies[i]['disctype']
            new_movie.hasnicetitle = movies[i]['hasnicetitle']
            new_movie.no_of_titles = movies[i]['no_of_titles']
            db.session.add(new_movie)
            i += 1
        else:
            sub_path = join(my_path, str(movie))
            # go through each folder and treat it as a subfolder of movie folder
            subfiles = [f for f in listdir(sub_path) if isfile(join(sub_path, f)) and not f.startswith(".")
                        or isdir(join(sub_path, f)) and not f.startswith(".")]
            for sub_movie in subfiles:
                mystring = f"{sub_movie}"
                sub_matched = re.match(regex, sub_movie)
                if sub_matched:
                    # This is only for pycharm
                    sub_movie_name = str.replace(" ", "%20", sub_matched.group(1).strip())  # movie
                    sub_movie_name = str.replace("&", "%26", sub_movie_name)
                    p2, imdb_id = get_omdb_poster(sub_movie_name, sub_matched.group(2))
                    app.logger.debug(listdir(join(sub_path, str(sub_movie))))
                    # If the user selects another ext thats not mkv we are f
                    sub_movie_files = [f for f in listdir(join(sub_path, str(sub_movie)))
                                       if isfile(join(sub_path, str(sub_movie), f)) and f.endswith("." + dest_ext)
                                       or isfile(join(sub_path, str(sub_movie), f)) and f.endswith(".mp4")
                                       or isfile(join(my_path, str(movie), f)) and f.endswith(".avi")]
                    app.logger.debug("movie files = " + str(sub_movie_files))
                    hash_object = hashlib.md5(mystring.encode())
                    dupe_found, not_used_variable = ui_utils.job_dupe_check(hash_object.hexdigest())
                    if dupe_found:
                        app.logger.debug("We found dupes breaking loop")
                        continue
                    movies[i] = {
                        'title': sub_matched.group(1),
                        'year': sub_matched.group(2),
                        'crc_id': hash_object.hexdigest(),
                        'imdb_id': imdb_id,
                        'poster': p2,
                        'status': 'success' if len(sub_movie_files) > 0 else 'fail',
                        'video_type': 'movie',
                        'disctype': 'unknown',
                        'hasnicetitle': True,
                        'no_of_titles': len(sub_movie_files)
                    }
                    new_movie = models.Job("/dev/sr0")
                    new_movie.title = movies[i]['title']
                    new_movie.year = movies[i]['year']
                    new_movie.crc_id = hash_object.hexdigest()
                    new_movie.imdb_id = imdb_id
                    new_movie.poster_url = p2
                    new_movie.status = movies[i]['status']
                    new_movie.video_type = movies[i]['video_type']
                    new_movie.disctype = movies[i]['disctype']
                    new_movie.hasnicetitle = movies[i]['hasnicetitle']
                    new_movie.no_of_titles = movies[i]['no_of_titles']
                    db.session.add(new_movie)
                    i += 1
                else:
                    movies[0]['notfound'][str(i)] = str(sub_movie)
            print(subfiles)
    # app.logger.debug(movies)

    time_1 = time.time()
    total = round(time_1 - time_0, 3)
    app.logger.debug(str(total) + " sec")
    db.session.commit()
    return app.response_class(response=json.dumps(movies, indent=4, sort_keys=True),
                              status=200,
                              mimetype=constants.JSON_TYPE)


@app.route('/send_movies', methods=['GET'])
@login_required
def send_movies():
    """
    function for sending all dvd crc64 ids to off-site api
    This isn't very optimised and can be slow and causes a huge number of requests
    """
    if request.args.get('s') is None:
        return render_template('send_movies_form.html')

    job_list = db.session.query(models.Job).filter_by(hasnicetitle=True, disctype="dvd").all()
    app.logger.debug("search - posts=" + str(job_list))
    return_dict = {}
    i = 0
    api_key = cfg['ARM_API_KEY']

    for job in job_list:
        # This allows easy updates to the API url
        # base_url = "https://1337server.pythonanywhere.com"
        # url = f"{base_url}/api/v1/?mode=p&api_key={api_key}&crc64={job.crc_id}&t={job.title}" \
        #       f"&y={job.year}&imdb={job.imdb_id}" \
        #       f"&hnt={job.hasnicetitle}&l={job.label}&vt={job.video_type}"
        # app.logger.debug(url.replace(api_key, ""))
        # response = requests.get(url)
        # req = json.loads(response.text)
        # app.logger.debug("req= " + str(req))
        job_dict = job.get_d().items()
        return_dict[i] = {}
        for key, value in iter(job_dict):
            return_dict[i][str(key)] = str(value)
        # if req['success']:
        #     return_dict[i]['status'] = "success"
        # else:
        #     return_dict[i]['Error'] = req['Error']
        #     return_dict[i]['status'] = "fail"
        i += 1
    return render_template('send_movies.html', full=return_dict)


@app.errorhandler(Exception)
def handle_exception(sent_error):
    """
    Exception handler
    :param sent_error: error
    :return: error page
    """
    # pass through HTTP errors
    if isinstance(sent_error, HTTPException):
        return sent_error

    app.logger.debug(f"Error: {sent_error}")
    if request.path.startswith('/json') or request.args.get('json'):
        app.logger.debug(f"{request.path} - {sent_error}")
        return_json = {
            'path': request.path,
            'Error': str(sent_error)
        }
        return app.response_class(response=json.dumps(return_json, indent=4, sort_keys=True),
                                  status=200,
                                  mimetype=constants.JSON_TYPE)

    return render_template(constants.ERROR_PAGE, error=sent_error), 500
