#!/usr/bin/python
"""Deploys Tabbycat to Heroku.
This script is compatible with both Python 2.7 and Python 3.4 (and later)."""

import argparse
import platform
import re
import subprocess
import sys

# Arguments
parser = argparse.ArgumentParser(description="Deploy Tabbycat to a new Heroku app.")

parser.add_argument(
    "urlname", type=str,
    help="Name of the Heroku app. The app will be at urlname.herokuapp.com. Use '-' to use a Heroku-generated default.")

parser.add_argument(
    "--no-open", action="store_false", default=True, dest="open",
    help="Don't open the Heroku website in your browser at the end")

parser.add_argument(
    "--git-remote", type=str, default=None,
    help="Name of Git remote to use. Use '-' to use the urlname. If omitted, reverts to default Heroku behaviour.")

parser.add_argument(
    "--git-branch", type=str, default=None,
    help="Git branch to push (defaults to current branch)")

parser.add_argument(
    "--pg-plan", "--postgresql-plan", type=str, default="hobby-dev",
    help="Heroku Postgres plan (default hobby-dev)")

parser.add_argument(
    "--web-dynos", type=str, default="1:free",
    help="Web dyno specification, passed to heroku ps:scale web=[], e.g. 1:free, 1:hobby, 2:Standard-1X")

parser.add_argument(
    "--import-tournament", type=str, metavar="IMPORT_DIR",
    help="Also run the importtournament command, importing from IMPORT_DIR")

parser.add_argument(
    "--dry-run", action="store_true", default=False,
    help="Print commands, don't run them.")

config_group = parser.add_argument_group("heroku configuration settings")
config_group.add_argument("--fast-cache-timeout", type=int, default=None, metavar="TIMEOUT",
                          help="Set the faster public page cache timeout to TIMEOUT")
config_group.add_argument("--slow-cache-timeout", type=int, default=None, metavar="TIMEOUT",
                          help="Set the slower public page cache timeout to TIMEOUT")
config_group.add_argument("--tab-cache-timeout", type=int, default=None, metavar="TIMEOUT",
                          help="Set the tab page cache timeout to TIMEOUT")
config_group.add_argument("--enable-debug", action="store_true", default=False,
                          help="Enable Django debug pages")

# Import tournament arguments are copied from importtournament.py, and should be
# updated when these options in importtournament.py change.
import_tournament_group = parser.add_argument_group(
    "import tournament options",
    "Passed to the importtournament command. Ignored unless " +
    "--import-tournament is used. Provided for convenience; to use other " +
    "importtournament options, run the importtournament command separately instead.")
import_tournament_group.add_argument(
    '-s', '--slug', type=str, action='store', default=None, dest="tournament_slug",
    help='Override tournament slug. (Default: use name of directory.)'),
import_tournament_group.add_argument(
    '--name', type=str, action='store', default=None, dest="tournament_name",
    help='Override tournament name. (Default: use name of directory.)'),
import_tournament_group.add_argument(
    '--short-name', type=str, action='store', default=None, dest="tournament_short_name",
    help='Override tournament short name. (Default: use name of directory.)'),

args = parser.parse_args()

if platform.system() == "Windows":
    subprocess_kwargs = dict(shell=True)
    use_color = False
else:
    subprocess_kwargs = dict()
    use_color = True

# Helper functions


def print_command(command):
    message = "$ " + " ".join(command)
    if use_color:
        message = "\033[1;36m" + message + "\033[0m"
    print(message)


def run_command(command):
    print_command(command)
    if not args.dry_run:
        subprocess.check_call(command, **subprocess_kwargs)


def make_heroku_command(command):
    return ["heroku"] + command + ["--app", urlname]


def run_heroku_command(command):
    command = make_heroku_command(command)
    run_command(command)


def get_output_from_command(command):
    print_command(command)
    output = subprocess.check_output(command, **subprocess_kwargs)
    output = output.decode()
    sys.stdout.write(output)
    sys.stdout.flush()
    return output


def print_yellow(message):
    if use_color:
        message = "\033[1;33m" + message + "\033[0m"
    print(message)


def get_git_push_spec():
    if args.git_branch:
        return "master" if args.git_branch == "master" else args.git_branch + ":master"
    try:
        branch = get_output_from_command(["git", "symbolic-ref", "--short", "--quiet", "HEAD"]).strip()
    except subprocess.CalledProcessError:
        print_yellow("Attempt to find git branch name failed, trying for commit instead...")
    else:
        return "master" if branch == "master" else branch + ":master"
    try:
        return get_output_from_command(["git", "rev-parse", "--short", "--quiet", "HEAD"]).strip() + ":refs/heads/master"
    except subprocess.CalledProcessError:
        print_yellow("Could not determine current git commit or branch. Use --git-branch to specify a git branch to push.")
    exit(1)

# Create the app with addons
addons = ["papertrail", "sendgrid:starter", "heroku-postgresql:%s" % args.pg_plan]
command = ["heroku", "apps:create"]

# Ensure on the right stack (for NGINX compatability)
command.extend(["--stack", "heroku-20"])

if addons:
    command.extend(["--addons", ",".join(addons)])
if args.urlname != "-":
    command.append(args.urlname)
output = get_output_from_command(command)
match = re.search("https://([\w_-]+)\.herokuapp\.com/\s+\|\s+(https://git.heroku.com/[\w_-]+.git)", output)
urlname = match.group(1)
heroku_url = match.group(2)

# Add the redis add-ons (the heroku one needs a config flag)
run_heroku_command(["addons:create", "heroku-redis:hobby-dev",
                    "--maxmemory_policy", "allkeys-lru"])
run_heroku_command(["addons:create", "rediscloud:30"])

# Set build packs
run_heroku_command(["buildpacks:set", "https://github.com/heroku/heroku-buildpack-nginx.git"])
run_heroku_command(["buildpacks:add", "--index", "1", "heroku/nodejs"])
run_heroku_command(["buildpacks:add", "--index", "2", "heroku/python"])

# Disable automatic collectstatic; do so on post_compile after gulp
command = ["config:add", "DISABLE_COLLECTSTATIC=1"]
run_heroku_command(command)

# Set config variables
command = ["config:add"]
command.append("DEBUG=1" if args.enable_debug else "DEBUG=0")
if args.fast_cache_timeout:
    command.append("PUBLIC_FAST_CACHE_TIMEOUT=%d" % args.fast_cache_timeout)
if args.slow_cache_timeout:
    command.append("PUBLIC_SLOW_CACHE_TIMEOUT=%d" % args.slow_cache_timeout)
if args.tab_cache_timeout:
    command.append("TAB_PAGES_CACHE_TIMEOUT=%d" % args.tab_cache_timeout)

run_heroku_command(command)

# Set up a remote, if applicable
if args.git_remote:
    remote_name = args.git_remote if args.git_remote != "-" else urlname
    run_heroku_command(["git:remote", "--remote", remote_name])
else:
    remote_name = heroku_url

# Push source code to Heroku
push_spec = get_git_push_spec()
run_command(["git", "push", remote_name, push_spec])

# Scale dynos (by default it only adds 1 web dyno)
run_heroku_command(["ps:scale", "web=%s" % args.web_dynos])

# Make secret key
command = make_heroku_command(["run", "python", "tabbycat/manage.py", "generate_secret_key"])
secret_key = get_output_from_command(command)
secret_key = secret_key.strip().split()[-1].strip()  # turn command output into string of just the key
print_yellow("Made secret key: \"%s\"" % secret_key)
command = ["config:add", "DJANGO_SECRET_KEY=%s" % secret_key]
run_heroku_command(command)

print_yellow("Now creating a superuser for the Heroku site.")
print_yellow("You'll need to respond to the prompts:")
run_heroku_command(["run", "python", "tabbycat/manage.py", "createsuperuser"])

# Import tournament, if provided
if args.import_tournament:
    command = ["run", "python", "tabbycat/manage.py", "importtournament", args.import_tournament]
    if args.tournament_slug:
        command += ["--slug", args.tournament_slug]
    if args.tournament_name:
        command += ["--name", args.tournament_name]
    if args.tournament_short_name:
        command += ["--short-name", args.tournament_short_name]
    run_heroku_command(command)

# Open in browser
if args.open:
    run_heroku_command(["open"])
