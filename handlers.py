import logging
from pprint import pprint, pformat
import tornado.auth
import tornado.web
import tornado.gen
#from tornado import gen
from tornado.web import HTTPError
from tornado_utils.routes import route
from tornado.escape import json_decode, json_encode
from pymongo.objectid import InvalidId, ObjectId
import utils
#import settings

from models import User


class BaseHandler(tornado.web.RequestHandler):

    def write_json(self, struct, javascript=False):
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(tornado.escape.json_encode(struct))

    def write_jsonp(self, callback, struct):
        self.set_header("Content-Type", "text/javascript; charset=UTF-8")
        self.write('%s(%s)' % (callback, tornado.escape.json_encode(struct)))

    def get_current_user(self):
        _id = self.get_secure_cookie('user')
        if _id:
            try:
                return self.db.User.find_one({'_id': ObjectId(_id)})
            except InvalidId:  # pragma: no cover
                return self.db.User.find_one({'username': _id})

    @property
    def redis(self):
        return self.application.redis

    @property
    def db(self):
        return self.application.db


@route('/')
class HomeHandler(BaseHandler):

    def get(self):
        options = {
          'page_title': 'Too Cool for Me?',
        }
        user = self.get_current_user()
        if user:
            url = '/static/bookmarklet.js'
            url = '%s://%s%s' % (self.request.protocol,
                                 self.request.host,
                                 url)
            options['full_bookmarklet_url'] = url

        options['user'] = user
        self.render('home.html', **options)


@route('/json', name='json')
@route('/jsonp', name='jsonp')
class FollowsHandler(BaseHandler, tornado.auth.TwitterMixin):

    def increment_lookup_count(self, username, usernames, jsonp=False):
        if jsonp:
            key = 'lookups:jsonp'
        else:
            key = 'lookups:json'
        if not isinstance(usernames, int):
            usernames = len(usernames)
        self.redis.incr(key)

        key = 'lookups:username:%s' % username
        self.redis.incr(key)

        key = 'lookups:usernames'
        self.redis.incr(key, usernames)

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self):
        jsonp = 'jsonp' in self.request.path

        if (self.get_argument('username', None) and
            not self.get_argument('usernames', None)):
            usernames = self.get_arguments('username')
        else:
            usernames = self.get_arguments('usernames')
        if isinstance(usernames, basestring):
            usernames = [usernames]
        elif (isinstance(usernames, list)
              and len(usernames) == 1
              and ',' in usernames[0]):
            usernames = [x.strip() for x in
                         usernames[0].split(',')
                         if x.strip()]
        # make sure it's a unique list
        usernames = set(usernames)

        if jsonp:
            self.jsonp = self.get_argument('callback', 'callback')
        else:
            self.jsonp = False

        if not usernames:
            msg = {'ERROR': 'No usernames asked for'}
            if jsonp:
                self.write_jsonp(self.jsonp, msg)
            else:
                self.write_json(msg)
            self.finish()
            return

        # All of this is commented out until I can figure out why cookie
        # headers aren't sent from bookmarklet's AJAX code
        this_username = self.get_argument('you', None)
        access_token = None
        if this_username is not None:
            user = self.db.User.find_one({'username': this_username})
            if user:
                access_token = user['access_token']
        else:
            user = self.get_current_user()
            if user:
                this_username = user['username']
                access_token = user['access_token']

        if not access_token:
            msg = {'ERROR': ('Not authorized. Go to http://%s and sign in' %
                              self.request.host)}
            if self.jsonp:
                self.write_jsonp(self.jsonp, msg)
            else:
                self.write_json(msg)
            self.finish()
            return

        self.increment_lookup_count(this_username, len(usernames), jsonp=jsonp)

        results = {}
        # pick some up already from the cache
        _drop = set()
        for username in usernames:
            key = 'follows:%s:%s' % (this_username, username)
            value = self.redis.get(key)
            if value is not None:
                results[username] = bool(int(value))
                _drop.add(username)
        usernames -= _drop

        if len(usernames) == 1:
            username = list(usernames)[0]
            # See https://dev.twitter.com/docs/api/1/get/friendships/show

            result = yield tornado.gen.Task(self.twitter_request,
                                    "/friendships/show",
                                    source_screen_name=this_username,
                                    target_screen_name=username,
                                    access_token=access_token)
            self._on_show(result, this_username, username, results)
        elif usernames:
            if len(usernames) > 100:
                raise HTTPError(400, "Too many usernames to look up (max 100)")
            # See https://dev.twitter.com/docs/api/1/get/friendships/lookup
            result = None
            attempts = 0
            while result is None:
                result = yield tornado.gen.Task(self.twitter_request,
                                        "/friendships/lookup",
                                        screen_name=','.join(usernames),
                                        access_token=access_token)
                if result is not None:
                    break
                else:
                    attempts += 1
                    from time import sleep
                    sleep(1)
                    if attempts > 2:
                        raise HTTPError(500, "Unable to look up friendships")
            self._on_lookup(result, this_username, results)
        else:
            # all usernames were lookup'able by cache
            if self.jsonp:
                self.write_jsonp(self.jsonp, results)
            else:
                self.write_json(results)
            self.finish()

    def _on_lookup(self, result, this_username, data):
        for each in result:
            if 'followed_by' in each['connections']:
                data[each['screen_name']] = True
            else:
                data[each['screen_name']] = False
            key = 'follows:%s:%s' % (this_username, each['screen_name'])
            self.redis.setex(key, int(data[each['screen_name']]), 60 * 5)

        if self.jsonp:
            self.write_jsonp(self.jsonp, data)
        else:
            self.write_json(data)
        self.finish()

    def _on_show(self, result, this_username, username, data):
        target_follows = None
        if result and 'relationship' in result:
            target_follows = result['relationship']['target']['following']
        key = 'follows:%s:%s' % (this_username, username)
        if target_follows is not None:
            self.redis.setex(key, int(bool(target_follows)), 60)
        data[username] = target_follows
        if self.jsonp:
            self.write_jsonp(self.jsonp, data)
        else:
            self.write_json(data)
        self.finish()


class BaseAuthHandler(BaseHandler):

    def get_next_url(self):
        return '/'


@route('/auth/twitter/', name='auth_twitter')
class TwitterAuthHandler(BaseAuthHandler, tornado.auth.TwitterMixin):

    def increment_authentication_count(self, username):
        key = 'auths:username:%s' % username
        self.redis.incr(key)
        key = 'auths:total'
        self.redis.incr(key)

    @tornado.web.asynchronous
    def get(self):
        if self.get_argument("oauth_token", None):
            self.get_authenticated_user(self.async_callback(self._on_auth))
            return
        self.authenticate_redirect()

    def _on_auth(self, user_struct):
        if not user_struct:
            options = {}
            options['page_title'] = "Twitter authentication failed"
            self.render('twitter_auth_failed.html', **options)
            return
        username = user_struct.get('username')
        access_token = user_struct['access_token']
        assert access_token
        user = self.db.User.find_one({'username': username})
        if user is None:
            user = self.db.User()
            user['username'] = username
            user['access_token'] = access_token
            user.save()

        self.increment_authentication_count(username)

        self.set_secure_cookie("user",
                               str(user['_id']),
                               expires_days=30, path='/')
        self.redirect('/')


@route(r'/auth/logout/', name='logout')
class AuthLogoutHandler(BaseAuthHandler):
    def get(self):
        self.clear_all_cookies()
        self.redirect(self.get_next_url())


@route(r'/test', name='test')
class TestServiceHandler(BaseHandler):

    def get(self):
        options = {}
        user = self.get_current_user()
        if not user:
            self.redirect('/auth/twitter/')
            return
        options['user'] = user
        options['page_title'] = "Test the service"
        self.render('test.html', **options)


@route('/following/(\w+)', name='following')
class FollowingHandler(BaseHandler, tornado.auth.TwitterMixin):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self, username):
        options = {'username': username}
        #this_username = self.get_current_user()
        current_user = self.get_current_user()
        if not current_user:
            self.redirect(self.reverse_url('auth_twitter'))
            return
        this_username = current_user['username']
        options['this_username'] = this_username
        options['follows'] = None
        key = 'follows:%s:%s' % (this_username, username)
        value = self.redis.get(key)
        if value is None:
            access_token = current_user['access_token']
            result = yield tornado.gen.Task(self.twitter_request,
                                    "/friendships/show",
                                    source_screen_name=this_username,
                                    target_screen_name=username,
                                    access_token=access_token)
        else:
            result = bool(int(value))
            key = None
        self._on_friendship(result, key, options)

    def _on_friendship(self, result, key, options):
        if result is None:
            options['error'] = ("Unable to look up friendship for %s" %
                                options['username'])
            self._render(options)
            return

        if isinstance(result, bool):
            value = result
        else:
            logging.info("Result (%r): %r" % (key, result))
            if result and 'relationship' in result:
                value = result['relationship']['target']['following']
                if key and value is not None:
                    self.redis.setex(key, int(bool(value)), 60)
        options['follows'] = value
        self._fetch_info(options)

    @tornado.gen.engine
    def _fetch_info(self, options, username=None):
        if username is None:
            username = options['username']

        key = 'info:%s' % username
        value = self.redis.get(key)

        if value is None:
            user = self.db.User.find_one({'username': options['this_username']})
            access_token = user['access_token']
            result = yield tornado.gen.Task(self.twitter_request,
                                            "/users/show",
                                            screen_name=username,
                                            access_token=access_token)
        else:
            result = json_decode(value)
            key = None
        self._on_info(result, key, options, username)

    def _on_info(self, result, key, options, username):
        if result is None:
            options['error'] = "Unable to look up info for %s" % username
            self._render(options)
            return
        if isinstance(result, basestring):
            result = json_decode(result)
        if key:
            self.redis.setex(key, json_encode(result), 60 * 60)
        if 'info' not in options:
            options['info'] = {options['username']: result}
            self._fetch_info(options, username=options['this_username'])
        else:
            options['info'][options['this_username']] = result
            self._render(options)

    def _render(self, options):
        if 'error' not in options:
            if options['follows']:
                page_title = '%s follows me'
            else:
                page_title = '%s is too cool for me'
            self._set_ratio(options, 'username')
            self._set_ratio(options, 'this_username')
            options['page_title'] = page_title % options['username']
            self.render('following.html', **options)
        else:
            options['page_title'] = 'Error :('
            self.render('following_error.html', **options)

    def _set_ratio(self, options, key):
        value = options[key]
        followers = options['info'][value]['followers_count']
        following = options['info'][value]['friends_count']
        ratio = 1.0 * followers / max(following, 1)
        options['info'][value]['ratio'] = '%.1f' % ratio
        self.redis.sadd('allusernames', value)
        key = 'ratios'
        self.redis.zadd(key, **{value: ratio})
        _usernames = self.redis.zrange(key, 0, -1, withscores=False)
        _usernames.reverse()
        options['info'][value]['rank'] = _usernames.index(value)



@route(r'/coolest', name='coolest')
class CoolestHandler(BaseHandler):  # pragma: no cover  (under development)

    def get(self):
        options = {}
        user = self.get_current_user()
        key = 'ratios'
        ratios = self.redis.zrange(key, 0, -1, withscores=True)
        ratios.reverse()
        options['ratios'] = ratios
        options['user'] = user
        options['page_title'] = \
          "Coolest in the world! ...on Twitter ...using this site"
        self.render('coolest.html', **options)

@route(r'/screenshots', name='screenshots')
class ScreenshotsHandler(BaseHandler):  # pragma: no cover  (under development)

    def get(self):
        options = {}
        options['page_title'] = "Screenshots"
        self.render('screenshots.html', **options)

@route('/everyone', name='everyone')
class EveryoneIFollowHandler(BaseHandler, tornado.auth.TwitterMixin):

    #@tornado.web.asynchronous
    #@tornado.gen.engine
    def get(self):
        current_user = self.get_current_user()
        if not current_user:
            self.redirect(self.reverse_url('auth_twitter'))
            return
        options = {}
        options['page_title'] = "Everyone I follow"
        self.render('everyone.html', **options)

@route('/everyone.json', name='everyone_json')
class EveryoneIFollowJSONHandler(BaseHandler, tornado.auth.TwitterMixin):

    @tornado.web.asynchronous
    @tornado.gen.engine
    def get(self):

        current_user = self.get_current_user()
        if not current_user:
            raise HTTPError(403, "Not logged in")

        this_username = current_user['username']
        access_token = current_user['access_token']
        key = 'friends:%s' % this_username
        result = self.redis.get(key)
        if result is None:
            result = yield tornado.gen.Task(self.twitter_request,
              "/friends/ids",
              screen_name=this_username,
              access_token=access_token
            )
            self.redis.setex(key, json_encode(result), 60 * 60)
        else:
            result = json_decode(result)
        # now turn these IDs into real screen names
        unknown = []
        screen_names = []
        for id_ in result:
            user = self.db.User.find_one({'user_id': id_})
            if user:
                screen_names.append(user['username'])
            else:
                key = 'screen_name:%s' % id_
                screen_name = self.redis.get(key)
                if screen_name is None:
                    unknown.append(id_)
                else:
                    screen_names.append(screen_name)

        buckets = utils.bucketize(unknown, 100)

        for bucket in buckets:
            users = None
            attempts = 0
            while True:
                users = yield tornado.gen.Task(self.twitter_request,
                  "/users/lookup",
                  user_id=','.join(str(x) for x in bucket)
                )
                if users is not None:
                    break
                else:
                    from time import sleep
                    sleep(1)
                    attempts += 1
                    if attempts > 3:
                        raise HTTPError(500, "Unable to connect to twitter")
            for user in users:
                username = user['screen_name']
                key = 'screen_name:%s' % user['id']
                self.redis.setex(key, username, 7 * 24 * 60 * 60)
                screen_names.append(username)

        assert len(result) == len(screen_names)

        screen_names.sort()
        self.write_json(screen_names)
        self.finish()


@route('/lookups', name='lookups')
class LookupsHandler(BaseHandler):

    def get_lookups(self, username=None):
        data = {}
        data['lookups_json'] = self.redis.get('lookups:json') or 0
        data['lookups_jsonp'] = self.redis.get('lookups:jsonp') or 0
        data['auths'] = self.redis.get('auths:total') or 0
        data['lookups_usernames'] = self.redis.get('lookups:usernames') or 0
        if username:
            print "NotImplmented"
        for key, value in data.items():
            data[key] = int(value)
        return data

    def get(self):
        options = {}
        options['page_title'] = "Lookups"
        options.update(self.get_lookups())
        self.render('lookups.html', **options)

@route('/lookups.json', name='lookups_json')
class LookupsJSONHandler(LookupsHandler):

    def get(self):
        data = self.get_lookups()
        self.write_json(data)
