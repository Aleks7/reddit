# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of
# the Original Code is reddit Inc.
#
# All portions of the code written by reddit are Copyright (c) 2006-2015 reddit
# Inc. All Rights Reserved.
###############################################################################

from collections import defaultdict
from datetime import datetime
import json

from pylons import tmpl_context as c, app_globals as g, request

from r2.lib import amqp, hooks
from r2.lib.eventcollector import Event
from r2.lib.utils import epoch_timestamp, is_subdomain, UrlParser
from r2.models import Account, Comment, Link, Subreddit
from r2.models.last_modified import LastModified
from r2.models.query_cache import CachedQueryMutator
from r2.models.vote import Vote, VotesByAccount

from r2.lib.geoip import organization_by_ips

def prequeued_vote_key(user, item):
    return 'queuedvote:%s_%s' % (user._id36, item._fullname)


def update_vote_lookups(user, thing, direction):
    """Store info about the existence of this vote (before processing)."""
    # set the vote in memcached so the UI gets updated immediately
    key = prequeued_vote_key(user, thing)
    grace_period = int(g.vote_queue_grace_period.total_seconds())
    direction = Vote.serialize_direction(direction)
    g.gencache.set(key, direction, time=grace_period+1)

    # update LastModified immediately to help us cull prequeued_vote lookups
    rel_cls = VotesByAccount.rel(thing.__class__)
    LastModified.touch(user._fullname, rel_cls._last_modified_name)


def cast_vote(user, thing, direction, **data):
    """Register a vote and queue it for processing."""
    update_vote_lookups(user, thing, direction)

    vote_data = {
        "user_id": user._id,
        "thing_fullname": thing._fullname,
        "direction": direction,
        "date": int(epoch_timestamp(datetime.now(g.tz))),
    }

    data['ip'] = getattr(request, "ip", None)
    if data['ip'] is not None:
        data['org'] = organization_by_ips(data['ip'])
    vote_data['data'] = data

    hooks.get_hook("vote.get_vote_data").call(
        data=vote_data["data"],
        user=user,
        thing=thing,
        request=request,
        context=c,
    )

    # The vote event will actually be sent from an async queue processor, so
    # we need to pull out the context data at this point
    if not g.running_as_script:
        vote_data["event_data"] = {
            "context": Event.get_context_data(request, c),
            "sensitive": Event.get_sensitive_context_data(request, c),
        }

    amqp.add_item(thing.vote_queue_name, json.dumps(vote_data))


def update_user_liked(vote):
    from r2.lib.db.queries import get_disliked, get_liked

    with CachedQueryMutator() as m:
        # if this is a changed vote, remove from the previous cached
        # query
        if vote.previous_vote:
            if vote.previous_vote.is_upvote:
                m.delete(get_liked(vote.user), [vote.previous_vote])
            elif vote.previous_vote.is_downvote:
                m.delete(get_disliked(vote.user), [vote.previous_vote])

        # and then add to the new cached query
        if vote.is_upvote:
            m.insert(get_liked(vote.user), [vote])
        elif vote.is_downvote:
            m.insert(get_disliked(vote.user), [vote])


def consume_link_vote_queue(qname="vote_link_q"):
    @g.stats.amqp_processor(qname)
    def process_message(msg):
        vote_data = json.loads(msg.body)
        hook = hooks.get_hook('vote.validate_vote_data')
        if hook.call_until_return(msg=msg, vote_data=vote_data) is False:
            # Corrupt records in the queue. Ignore them.
            print "Ignoring invalid vote by %s on %s %s" % (
                    vote_data.get('user_id', '<unknown>'),
                    vote_data.get('thing_fullname', '<unknown>'),
                    vote_data)
            return

        timer = g.stats.get_timer("link_vote_processor")
        timer.start()

        user = Account._byID(vote_data.pop("user_id"))
        link = Link._by_fullname(vote_data.pop("thing_fullname"))

        # create the vote and update the voter's liked/disliked under lock so
        # that the vote state and cached query are consistent
        lock_key = "vote-%s-%s" % (user._id36, link._fullname)
        with g.make_lock("voting", lock_key, timeout=5):
            print "Processing vote by %s on %s %s" % (user, link, vote_data)

            try:
                vote = Vote(
                    user,
                    link,
                    direction=vote_data["direction"],
                    date=datetime.utcfromtimestamp(vote_data["date"]),
                    data=vote_data["data"],
                    event_data=vote_data.get("event_data"),
                )
            except TypeError as e:
                # a vote on an invalid type got in the queue, just skip it
                g.log.exception("Invalid type: %r", e.message)
                return

            vote.commit()
            timer.intermediate("create_vote_object")

            update_user_liked(vote)
            timer.intermediate("voter_likes")

        vote_valid = vote.is_automatic_initial_vote or vote.effects.affects_score
        link_valid = not (link._spam or link._deleted)
        if vote_valid and link_valid:
            add_to_author_query_q(link)
            add_to_subreddit_query_q(link)
            add_to_domain_query_q(link)

        timer.stop()
        timer.flush()

    amqp.consume_items(qname, process_message, verbose=False)


# these sorts can be changed by voting - we don't need to do "new" since that's
# taken care of by new_link and doesn't change afterwards
SORTS = ["hot", "top", "controversial"]


def add_to_author_query_q(link):
    if g.shard_author_query_queues:
        author_shard = link.author_id % 10
        queue_name = "author_query_%s_q" % author_shard
    else:
        queue_name = "author_query_q"
    amqp.add_item(queue_name, link._fullname)


def consume_author_query_queue(qname="author_query_q", limit=1000):
    @g.stats.amqp_processor(qname)
    def process_message(msgs, chan):
        """Update get_submitted(), the Links by author precomputed query.

        get_submitted() is a CachedResult which is stored in permacache. To
        update these objects we need to do a read-modify-write which requires
        obtaining a lock. Sharding these updates by author allows us to run
        multiple consumers (but ideally just one per shard) to avoid lock
        contention.

        """

        from r2.lib.db.queries import add_queries, get_submitted

        link_names = {msg.body for msg in msgs}
        links = Link._by_fullname(link_names, return_dict=False)
        print 'Processing %r' % (links,)

        links_by_author_id = defaultdict(list)
        for link in links:
            links_by_author_id[link.author_id].append(link)

        authors_by_id = Account._byID(links_by_author_id.keys())

        for author_id, links in links_by_author_id.iteritems():
            with g.stats.get_timer("link_vote_processor.author_queries"):
                author = authors_by_id[author_id]
                add_queries(
                    queries=[
                        get_submitted(author, sort, 'all') for sort in SORTS],
                    insert_items=links,
                )

    amqp.handle_items(qname, process_message, limit=limit)


def add_to_subreddit_query_q(link):
    if g.shard_subreddit_query_queues:
        subreddit_shard = link.sr_id % 10
        queue_name = "subreddit_query_%s_q" % subreddit_shard
    else:
        queue_name = "subreddit_query_q"
    amqp.add_item(queue_name, link._fullname)


def consume_subreddit_query_queue(qname="subreddit_query_q", limit=1000):
    @g.stats.amqp_processor(qname)
    def process_message(msgs, chan):
        """Update get_links(), the Links by Subreddit precomputed query.

        get_links() is a CachedResult which is stored in permacache. To
        update these objects we need to do a read-modify-write which requires
        obtaining a lock. Sharding these updates by subreddit allows us to run
        multiple consumers (but ideally just one per shard) to avoid lock
        contention.

        """

        from r2.lib.db.queries import add_queries, get_links

        link_names = {msg.body for msg in msgs}
        links = Link._by_fullname(link_names, return_dict=False)
        print 'Processing %r' % (links,)

        links_by_sr_id = defaultdict(list)
        for link in links:
            links_by_sr_id[link.sr_id].append(link)

        srs_by_id = Subreddit._byID(links_by_sr_id.keys(), stale=True)

        for sr_id, links in links_by_sr_id.iteritems():
            with g.stats.get_timer("link_vote_processor.subreddit_queries"):
                sr = srs_by_id[sr_id]
                add_queries(
                    queries=[get_links(sr, sort, "all") for sort in SORTS],
                    insert_items=links,
                )

    amqp.handle_items(qname, process_message, limit=limit)


def add_to_domain_query_q(link):
    parsed = UrlParser(link.url)
    if not parsed.domain_permutations():
        # no valid domains found
        return

    if g.shard_domain_query_queues:
        domain_shard = hash(parsed.hostname) % 10
        queue_name = "domain_query_%s_q" % domain_shard
    else:
        queue_name = "domain_query_q"
    amqp.add_item(queue_name, link._fullname)


def consume_domain_query_queue(qname="domain_query_q", limit=1000):
    @g.stats.amqp_processor(qname)
    def process_message(msgs, chan):
        """Update get_domain_links(), the Links by domain precomputed query.

        get_domain_links() is a CachedResult which is stored in permacache. To
        update these objects we need to do a read-modify-write which requires
        obtaining a lock. Sharding these updates by domain allows us to run
        multiple consumers (but ideally just one per shard) to avoid lock
        contention.

        """

        from r2.lib.db.queries import add_queries, get_domain_links

        link_names = {msg.body for msg in msgs}
        links = Link._by_fullname(link_names, return_dict=False)
        print 'Processing %r' % (links,)

        links_by_domain = defaultdict(list)
        for link in links:
            parsed = UrlParser(link.url)

            # update the listings for all permutations of the link's domain
            for domain in parsed.domain_permutations():
                links_by_domain[domain].append(link)

        for d, links in links_by_domain.iteritems():
            with g.stats.get_timer("link_vote_processor.domain_queries"):
                add_queries(
                    queries=[
                        get_domain_links(d, sort, "all") for sort in SORTS],
                    insert_items=links,
                )

    amqp.handle_items(qname, process_message, limit=limit)


def consume_comment_vote_queue(qname="vote_comment_q"):
    @g.stats.amqp_processor(qname)
    def process_message(msg):
        from r2.lib.db.queries import (
            add_queries,
            add_to_commentstree_q,
            get_comments,
        )

        vote_data = json.loads(msg.body)
        hook = hooks.get_hook('vote.validate_vote_data')
        if hook.call_until_return(msg=msg, vote_data=vote_data) is False:
            # Corrupt records in the queue. Ignore them.
            print "Ignoring invalid vote by %s on %s %s" % (
                    vote_data.get('user_id', '<unknown>'),
                    vote_data.get('thing_fullname', '<unknown>'),
                    vote_data)
            return

        timer = g.stats.get_timer("comment_vote_processor")
        timer.start()

        user = Account._byID(vote_data.pop("user_id"))
        comment = Comment._by_fullname(vote_data.pop("thing_fullname"))

        print "Processing vote by %s on %s %s" % (user, comment, vote_data)

        try:
            vote = Vote(
                user,
                comment,
                direction=vote_data["direction"],
                date=datetime.utcfromtimestamp(vote_data["date"]),
                data=vote_data["data"],
                event_data=vote_data.get("event_data"),
            )
        except TypeError as e:
            # a vote on an invalid type got in the queue, just skip it
            g.log.exception("Invalid type: %r", e.message)
            return

        vote.commit()
        timer.intermediate("create_vote_object")

        vote_valid = vote.is_automatic_initial_vote or vote.effects.affects_score
        comment_valid = not (comment._spam or comment._deleted)
        if vote_valid and comment_valid:
            author = Account._byID(comment.author_id)
            add_queries(
                queries=[get_comments(author, sort, 'all') for sort in SORTS],
                insert_items=comment,
            )
            timer.intermediate("author_queries")

            # update the score periodically when a comment has many votes
            update_threshold = g.live_config['comment_vote_update_threshold']
            update_period = g.live_config['comment_vote_update_period']
            num_votes = comment.num_votes
            if num_votes <= update_threshold or num_votes % update_period == 0:
                add_to_commentstree_q(comment)

        timer.stop()
        timer.flush()

    amqp.consume_items(qname, process_message, verbose=False)
