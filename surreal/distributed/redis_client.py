import redis
import threading
import itertools
import surreal.utils as U


class _DequeueThread(U.StoppableThread):
    def __init__(self, redis_client, queue_name, handler, **kwargs):
        """
        Args:
            redis_client:
            queue_name:
            handler:
            **kwargs:
        """
        self._client = redis_client
        self._queue_name = queue_name
        self._handler = handler
        super().__init__(**kwargs)

    def run(self):
        for i in itertools.count():
            if self.is_stopped():
                break
            # ignore queue name
            _, binary = self._client.brpop(self._queue_name)
            self._handler(binary, i)


class RedisClient(object):
    def __init__(self, host='localhost', port=6379):
        self._client = redis.StrictRedis(host=host, port=port)
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        self._queue_threads = {}
        self._subscribe_threads = {}

        # delegated method
        self.set = self._client.set
        self.get = self._client.get
        self.flushall = self._client.flushall

    def mset(self, mset_dict):
        U.assert_type(mset_dict, dict)
        if len(mset_dict) == 0:
            return False
        else:
            return self._client.mset(mset_dict)

    def mget(self, mget_list):
        U.assert_type(mget_list, list)
        if len(mget_list) == 0:
            return []
        else:
            return self._client.mget(mget_list)

    def enqueue(self, queue_name, binary):
        self._client.lpush(queue_name, binary)

    def start_dequeue_thread(self, queue_name, handler):
        """
        Forks a thread that runs in an infinite loop, listens on a Redis list
        Args:
          queue_name
          handler: does something upon receiving an object
            [binary_data, index] -> None
        """
        if queue_name in self._queue_threads:
            raise RuntimeError('Queue thread [{}] is already running'
                               .format(queue_name))
        t = _DequeueThread(self._client, queue_name, handler)
        self._queue_threads[queue_name] = t
        t.start()
        return t

    def stop_dequeue_thread(self, queue_name):
        self._queue_threads[queue_name].stop()

    def publish(self, channel, msg):
        self._client.publish(channel, msg)

    def start_subscribe_thread(self, channel, handler, sleep_time=0.1):
        """
        handler: function takes an incoming msg from the subscribed channel

        Every message read from a PubSub instance will be a dictionary with the following keys.
        type: One of the following: 'subscribe', 'unsubscribe', 'psubscribe',
            'punsubscribe', 'message', 'pmessage'
        channel: The channel [un]subscribed to or the channel a message was published to
        pattern: The pattern that matched a published message's channel.
            Will be None in all cases except for 'pmessage' types.
        data: The message data. With [un]subscribe messages, this value will be
            the number of channels and patterns the connection is currently
            subscribed to. With [p]message messages,
            this value will be the actual published message.
        """
        self._pubsub.subscribe(**{channel: handler})
        t = self._pubsub.run_in_thread(sleep_time=sleep_time)
        self._subscribe_threads[channel] = t
        return t

    def stop_subscribe_thread(self, channel):
        self._subscribe_threads[channel].stop()
