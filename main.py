#!/usr/bin/env python

from __future__ import division, print_function

import logging
import os.path
import urllib

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2
from webapp2_extras import sessions

import formencode
import formencode_jinja2

CONFIG = {
    'webapp2_extras.sessions': {
        # TODO: read from config that is not in git
        'secret_key': 'super-secret-key',
    },
}

# Regex to use for time
REGEX_TIME = r'^(\d+:)?\d+:\d+'
# Regex to use race
REGEX_RACE = r'^(6km|12km)$'
# Regex to use for male/female
REGEX_GENDER = r'^(male|female)$'

# TODO: input validation, non-duplicate starting no, use https://pypi.python.org/pypi/schema
# German errors
# TODO: transactions
# TODO: display number of finished and non-finished runners
# TODO: use CSS for prettifying things
# TODO: add reports


def display_seconds(seconds):
    """Return string displaying seconds as (hours), minutes and seconds
    """
    if seconds is None:
        return seconds

    tmp = int(seconds)
    secs = tmp % 60
    tmp = tmp // 60
    mins = tmp % 60
    tmp = tmp // 60
    hours = tmp

    if hours:
        return '{:02}:{:02}:{:02}'.format(hours, mins, secs)
    else:
        return '{:02}:{:02}'.format(mins, secs)


JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    extensions=['jinja2.ext.autoescape',
                'formencode_jinja2.formfill'],
    undefined=jinja2.StrictUndefined,
    autoescape=True)
JINJA_ENVIRONMENT.filters['display_seconds'] = display_seconds

formencode.api.set_stdtranslation(domain='FormEncode', languages=['de'])


# Default organization name.
DEFAULT_ORGANIZATION = 'sf_lotte'


# Used for trick with pseudo ancestor for real consistency
def organization_key(orga_name=DEFAULT_ORGANIZATION):
    return ndb.Key('Organization', orga_name)


class RunnerFinishedForm(formencode.Schema):
    """Form validation schema for putting time to runner"""

    allow_extra_fields = True
    filter_extra_fields = True
    start_no = formencode.validators.Int(not_empty=True, min=1)
    time = formencode.validators.Regex(REGEX_TIME, strip=True)


class EventForm(formencode.Schema):
    """Form validation schema for Event class"""

    allow_extra_fields = True
    filter_extra_fields = True
    title = formencode.validators.UnicodeString(not_empty=True)
    year = formencode.validators.Int(not_empty=True, min=1990, max=2500)
    next_start_no = formencode.validators.Int(not_empty=True, min=1)


class Event(ndb.Model):
    """Model for one event such as 'Volkslauf 2011'"""

    date = ndb.DateTimeProperty(auto_now_add=True)
    year = ndb.IntegerProperty(indexed=False)
    title = ndb.StringProperty(indexed=False)
    next_start_no = ndb.IntegerProperty()

    def all_runners(self):
        """Return Query with all Runner objects for this event"""
        return Runner.query(Runner.event == self.key,
                            ancestor=self.key).order(Runner.start_no)

    def num_runners(self):
        return self.all_runners().count()


class RunnerFormEncodeState(object):
    """State used for the RunnerForm

    Required for pulling the event_key and runner_key through the
    validation code.
    """
    
    def __init__(self, event_key, runner_key=None):
        self.event_key = event_key
        self.runner_key = runner_key


class UniqueStartNoValidator(formencode.FancyValidator):
   
    messages = {
        'exists': 'Die Startnr. wurde doppelt vergeben',
    }

    def validate_python(self, value, state):
        runner = Runner.query(Runner.start_no == value,
                              ancestor=state.event_key).get()
        if runner and runner.key != state.runner_key:
            raise formencode.Invalid(self.message('exists', state),
                                     value, state)
        return value


class RunnerForm(formencode.Schema):
    """Form validation schema for Runner class"""

    allow_extra_fields = True
    filter_extra_fields = True

    start_no = formencode.compound.Pipe(
            formencode.validators.Int(not_empty=True, min=1),
            UniqueStartNoValidator(not_empty=True))
    name = formencode.validators.UnicodeString(not_empty=True, max_len=200)
    team = formencode.validators.UnicodeString(max_len=200)
    gender = formencode.validators.Regex(REGEX_GENDER, not_empty=True)
    birth_year = formencode.validators.Int(not_empty=True, min=1900)
    race = formencode.validators.Regex(REGEX_RACE, not_empty=True, strip=True)


class Runner(ndb.Model):
    """Model for a participant in an Event"""

    event = ndb.KeyProperty(kind=Event, indexed=True)

    start_no = ndb.IntegerProperty(indexed=True)
    date = ndb.DateTimeProperty(auto_now_add=True)
    name = ndb.StringProperty(indexed=True)
    team = ndb.StringProperty(indexed=True)
    gender = ndb.StringProperty(indexed=False)
    birth_year = ndb.IntegerProperty(indexed=False)
    age_class = ndb.StringProperty(indexed=False)
    time = ndb.IntegerProperty(indexed=False)
    race = ndb.StringProperty(indexed=False)

    def _pre_put_hook(self):
        age_class = self._compute_age_class()
        if age_class:
            self.age_class = age_class

    def _compute_age_class(self):
        if not self.birth_year or self.gender not in ['male', 'female']:
            return None
        age = self.event.get().year - self.birth_year

        gender = 'M' if self.gender == 'male' else 'W'
        result = gender

        if age < 16:
            result += 'S '
            if age <= 9:
                result += 'D'
            elif age <= 11:
                result += 'C'
            elif age <= 13:
                result += 'B'
            elif age <= 15:
                result += 'A'
        elif age < 20:
            result += 'JG '
            if age <= 17:
                result += 'B'
            elif age <= 19:
                result += 'A'
        else:
            result = 'L' + gender

            if age <= 29:
                result += '20'
            elif age <= 34:
                result += '30'
            elif age <= 39:
                result += '35'
            elif age <= 44:
                result += '40'
            elif age <= 49:
                result += '45'
            elif age <= 54:
                result += '50'
            elif age <= 59:
                result += '55'
            elif age <= 64:
                result += '60'
            elif age <= 69:
                result += '65'
            else:
                result += '70'
        return result


class BaseHandler(webapp2.RequestHandler):
    """Base class for actual RequestHandler implementations

    Provides basic functionality such as easier template rendering
    and requiring authentication.
    """

    def dispatch(self):
        # Get a session store for this request
        self.session_store = sessions.get_store(request=self.request)
        try:
            # Dispatch the request
            webapp2.RequestHandler.dispatch(self)
        finally:
            # Save all sessions
            self.session_store.save_sessions(self.response)

    @webapp2.cached_property
    def session(self):
        """Return a session using the default cookie key"""
        return self.session_store.get_session()

    def _render(self, tpl_path, tpl_values):
        """Render HTML template at tpl_path with tpl_values to the client
        """
        tpl_values2 = dict(self._default_tpl_values())
        tpl_values2.update(tpl_values)
        template = JINJA_ENVIRONMENT.get_template(tpl_path)
        self.response.write(template.render(tpl_values2))

    def _default_tpl_values(self):
        """"Return dict with default template values"""
        vals = {
            'user': users.get_current_user(),
            'logout_url': users.create_logout_url(self.request.uri),
            'flash_info': self.session.get_flashes(key='info'),
            'flash_error': self.session.get_flashes(key='error'),
        }
        logging.info('vals={}'.format(vals))
        return vals


class EventListHandler(BaseHandler):
    """Handler for listing all events
    
    This is also used for the start page.
    """

    def get(self):
        all_events = list(Event.query(ancestor=organization_key()))
        vals = {'events': all_events}
        self._render('event/list.html', vals)


class EventCreateHandler(BaseHandler):
    """Handler for creating events
    
    Display creation mask on GET and actually create on POST (after validation
    of course).
    """

    def get(self):
        vals = {'event': Event().to_dict()}
        self._render('event/create.html', vals)

    def post(self):
        if not self.request.get('submit_create'):
            self.redirect('/event/list')
            return

        try:
            form = EventForm()
            form_result = form.to_python(dict(self.request.params))
            event = Event(parent=organization_key(), **form_result)
            event_key = event.put()
            self.redirect('/event/view/{}'.format(event_key.urlsafe()))
        except formencode.Invalid, e:
            self._render('event/create.html',
                         {'event': e.value, 'errors': e.error_dict})


class EventUpdateHandler(BaseHandler):
    """Handler for updating events
    
    Display update mask on GET and actually update on POST (after validation
    of course).
    """

    def get(self, event_key):
        vals = {'event': ndb.Key(urlsafe=event_key).get().to_dict()}
        self._render('event/update.html', vals)

    def post(self, event_key):
        if not self.request.get('submit_update'):
            self.redirect('/event/view/{}'.format(event_key))
            return

        event_key = ndb.Key(urlsafe=event_key)
        event = event_key.get()

        try:
            form = EventForm()
            event.populate(**form.to_python(dict(self.request.params)))
            event.put()
            self.redirect('/event/view/{}'.format(event_key.urlsafe()))
        except formencode.Invalid, e:
            self._render('event/update.html',
                         {'event': e.value, 'errors': e.error_dict})


class EventViewHandler(BaseHandler):
    """Handler for viewing one event"""

    def get(self, event_key):
        event = ndb.Key(urlsafe=event_key).get()
        self._render('event/view.html', {'event': event})


class EventDeleteHandler(BaseHandler):
    """Handler for deleting one event"""

    def get(self, event_key):
        event = ndb.Key(urlsafe=event_key).get()
        self._render('event/delete.html', {'event': event})

    def post(self, event_key):
        if self.request.get('submit_yes'):
            event_key = ndb.Key(urlsafe=event_key)
            event_key.get().delete()
            self.redirect('/event/list')
        else:
            self.redirect('/event/view/{}'.format(event_key))


class RunnerCreateHandler(BaseHandler):
    """Handler for creating a new runner"""

    def get(self, event_key):
        event_key = ndb.Key(urlsafe=event_key)
        event = event_key.get()
        runner = Runner(parent=event_key,
                        event=event_key,
                        start_no=event.next_start_no)
        vals = {'event': event.to_dict(), 'runner': runner.to_dict()}
        self._render('runner/create.html', vals)

    def post(self, event_key):
        if not self.request.get('submit_create'):
            self.redirect('/event/view/{}'.format(event_key))
            return

        event_key = ndb.Key(urlsafe=event_key)

        try:
            self._create_runner(event_key)
            self.redirect('/event/view/{}'.format(event_key.urlsafe()))
        except formencode.Invalid, e:
            logging.info(e.error_dict)
            self._render('runner/create.html',
                         {'runner': e.value,
                          'event': event_key.get().to_dict(),
                          'errors': e.error_dict})

    @ndb.transactional
    def _create_runner(self, event_key):
        """Create runner in a transactional fashion

        Takes care that no two runners with the same start number can exist and
        that the next start no of the owning event is updated.
        """
        state = RunnerFormEncodeState(event_key)

        form = RunnerForm()
        form_result = form.to_python(dict(self.request.params), state)

        #start_no_validator.to_python(self.request.get('start_no'))

        # Update next event start no if the same as for the event
        event = event_key.get()
        if event.next_start_no == form_result['start_no']:
            event.next_start_no += 1
            event.put()

        runner = Runner(parent=event_key,
                        event=event_key,
                        **form_result)
        return runner.put()


class RunnerUpdateHandler(BaseHandler):
    """Handler for updating details of a runner"""

    def get(self, event_key, runner_key):
        runner_key = ndb.Key(urlsafe=runner_key)
        event_key = ndb.Key(urlsafe=event_key)
        vals = {'runner': runner_key.get().to_dict(),
                'event': event_key.get()}
        self._render('runner/update.html', vals)

    def post(self, event_key, runner_key):
        runner_key = ndb.Key(urlsafe=runner_key)

        event_key = ndb.Key(urlsafe=event_key)
        
        try:
            self._update_runner(event_key, runner_key)
            self.redirect('/event/view/{}'.format(event_key.urlsafe()))
        except formencode.Invalid, e:
            self._render('runner/update.html',
                         {'runner': e.value,
                          'errors': e.error_dict,
                          'event': event_key.get()})

    @ndb.transactional
    def _update_runner(self, event_key, runner_key):
        """Update runner in a transactional fashion

        Validation must be done in a transaction against race conditions
        """
        runner = runner_key.get()
        state = RunnerFormEncodeState(event_key, runner_key)
        form = RunnerForm()
        runner.populate(**form.to_python(dict(self.request.params),
                                         state))
        return runner.put()


class RunnerViewHandler(BaseHandler):
    """Handler for viewing details of a runner"""

    def get(self, event_key, runner_key):
        runner = ndb.Key(urlsafe=runner_key).get()
        event = ndb.Key(urlsafe=event_key).get()
        self._render('runner/view.html', {'event': event, 'runner': runner})


class RunnerDeleteHandler(BaseHandler):
    """Handler for deleting a runner"""

    def get(self, event_key, runner_key):
        event = ndb.Key(urlsafe=event_key).get()
        runner = ndb.Key(urlsafe=runner_key).get()
        vals = {'event': event, 'runner': runner}
        self._render('runner/delete.html', vals)

    def post(self, event_key, runner_key):
        if self.request.get('submit_yes'):
            runner_key = ndb.Key(urlsafe=runner_key)
            runner_key.delete()
        self.redirect('/event/view/{}'.format(event_key))


def get_seconds_from_time(time_str):
    if not time_str:
        return None
    arr = list(map(int, time_str.split(':', 3)))
    if len(arr) == 3:
        seconds = arr[0] * 60 * 60 + arr[1] * 60 + arr[0]
    elif len(arr) == 2:
        seconds = arr[0] * 60 + arr[1]
    else:
        seconds = arr[0]
    return seconds


class RunnerFinishedHandler(BaseHandler):
    """Handler for a runner finishing"""

    def post(self, event_key):
        event_key = ndb.Key(urlsafe=event_key)

        msg = None

        try:
            form = RunnerFinishedForm()
            vals = form.to_python(dict(self.request.params))

            if not vals['time']:
                msg = ('Du musst den Laeufer bearbeiten um die Zeit loeschen '
                       'zu koennen, Laufzeiterfassung geht dafuer nicht!')
                self.session.add_flash(msg, key='error')
                self.redirect('/event/view/{}'.format(event_key.urlsafe()))
                return

            runners = Runner.query(Runner.start_no == vals['start_no'],
                                   ancestor=event_key)
            runner = runners.get()
            if runner:
                runner.time = get_seconds_from_time(self.request.get('time'))
                runner.put()
                msg = 'Zeit fuerr Laeufer {} gesetzt.'.format(runner.name)
                self.session.add_flash(msg, key='info')
        except formencode.Invalid, e:
            pass

        if not msg:
            msg = 'Konnte Zeit nicht setzen!'
            self.session.add_flash(msg, key='error')

        self.redirect('/event/view/{}'.format(event_key.urlsafe()))


ROUTE_LIST = [
    ('/', EventListHandler),
    ('/event/list', EventListHandler),
    ('/event/create', EventCreateHandler),
    ('/event/view/<event_key>', EventViewHandler),
    ('/event/update/<event_key>', EventUpdateHandler),
    ('/event/delete/<event_key>', EventDeleteHandler),
    ('/runner/<event_key>/create', RunnerCreateHandler),
    ('/runner/<event_key>/update/<runner_key>', RunnerUpdateHandler),
    ('/runner/<event_key>/view/<runner_key>', RunnerViewHandler),
    ('/runner/<event_key>/delete/<runner_key>', RunnerDeleteHandler),
    ('/runner/<event_key>/finished', RunnerFinishedHandler),
]
ROUTES = [webapp2.Route(*list(x)) for x in ROUTE_LIST]

app = webapp2.WSGIApplication(ROUTES, debug=True, config=CONFIG)
