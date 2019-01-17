import asyncio
import threading
import logging
import time
import inspect
import multiprocessing
import queue
import signal


class SubscriptionClosed(Exception):
    """
    This error is returned internally by :func:`_get` in all subclasses of :class:`BaseSubscriptionConsumer`
    when :func:`close` is called, and signals the consumer to shut down. For more detail, see :func:`BaseSubscriptionConsumer._get`.
    """

    pass


class BaseSubscriptionProducer:
    """
    This is a base class upon which all things that emit data in RTCBot are built.

    This class offers all the machinery necessary to keep track of subscriptions to the incoming data.
    The most important methods from a user's perspective are the :func:`subscribe`, :func:`get` and :func:`close` functions,
    which manage subscriptions to the data, and finally close everything.

    From an subclass's perspective, the most important pieces are the :func:`_put_nowait` method,
    and the :attr:`_shouldClose` and :attr:`_ready` attributes.

    Once the subclass is ready, it should set :attr:`_ready` to True, and when receiving data,
    it should call :func:`_put_nowait` to insert it. Finally, it should either listen to :attr:`_shouldClose` or override
    the close method to stop producing data.

    Example:
        A sample basic class that builds on the :class:`BaseSubscriptionProvider`::

            class MyProvider(BaseSubscriptionProvider):
                def __init__(self):
                    super().__init__()

                    # Add data in the background
                    asyncio.ensure_future(self._dataProducer)

                async def _dataProducer(self):
                    self._ready = True
                    while not self._shouldClose:
                        data = await get_data_here()
                        self._put_nowait(data)
                    self._ready = False
                def close():
                    super().close()
                    stop_gathering_data()

            # you can now subscribe to the data
            s = MyProvider().subscribe()

    Args:
        defaultSubscriptionClass (optional):
            The subscription type to return by default if :func:`subscribe` is called without arguments.
            By default, it uses :class:`asyncio.Queue`::

                sp = SubscriptionProducer(defaultSubscriptionClass=asyncio.Queue)
                q = sp.subscribe()

                q is asyncio.Queue # True
        defaultAutosubscribe (bool,optional):
            Calling :func:`get` creates a default subscription on first time it is called. Sometimes the data is very critical,
            and you want the default subscription to be created right away, so it never misses data. Be aware,
            though, if your `defaultSubscriptionClass` is :class:`asyncio.Queue`, if :func:`get` is never called,
            such as when someone just uses :func:`subscribe`, it will just keep piling up queued data!
            To avoid this, it is `False` by default.
        logger (optional):
            Your class logger - it gets a child of this logger for debug messages. If nothing is passed,
            creates a root logger for your class, and uses a child for that.
        ready (bool,optional):
            Your producer probably doesn't need setup time, so this is set to `True` automatically, which 
            automatically sets :attr:`_ready`. If you need to do background tasks, set this to False.
    """

    def __init__(
        self,
        defaultSubscriptionClass=asyncio.Queue,
        defaultAutosubscribe=False,
        logger=None,
        ready=True,
    ):
        self.__subscriptions = set()
        self.__callbacks = set()
        self.__cocallbacks = set()
        self.__defaultSubscriptionClass = defaultSubscriptionClass
        self.__defaultSubscription = None

        #: Whether or not :func:`close` was called, and the user wants the class to stop
        #: gathering data. Should only be accessed from a subclass.
        self._shouldClose = False

        if logger is None:
            self.__splog = logging.getLogger(self.__class__.__name__).getChild(
                "SubscriptionProducer"
            )
        else:
            self.__splog = logger.getChild("SubscriptionProducer")

        #: This needs to be manually set to :code:`True` once the producer has finished initialization and is ready to receive data.
        #: It should only be called by the subclass, and set before sending any data.
        self._ready = ready

        if defaultAutosubscribe:
            self.__defaultSubscribe()

    def subscribe(self, subscription=None):
        """
        Subscribes to new data as it comes in, returning a subscription (see :doc:`subscriptions`)::

            s = myobj.subscribe()
            while True:
                data = await s.get()
                print(data)

        There can be multiple independent subscriptions active at the same time. 
        Each call to :func:`subscribe` returns a new, independent subscription::

            s1 = myobj.subscribe()
            s2 = myobj.subscribe()
            while True:
                assert await s1.get()== await s2.get()


        Args:
            subscription (optional):
                An optional existing subscription to subscribe to. This can be one of 3 things:
                    1) An object which has the method `put_nowait` (see :doc:`subscriptions`)::
                        
                        q = asyncio.Queue()
                        myobj.subscribe(q)
                        while True:
                            data = await q.get()
                            print(data)
                    2) A callback function - this will be called the moment new data is inserted::
                        
                        @myobj.subscribe
                        def myfunction(data):
                            print(data)
                    3) An coroutine callback - A future of this coroutine is created on each insert::
                        
                        @myobj.subscribe
                        async def myfunction(data):
                            await asyncio.sleep(5)
                            print(data)
                    
        Returns:
            A subscription. If one was passed in, returns the passed in subscription::

                q = asyncio.Queue()
                ret = thing.subscribe(q)
                assert ret==q

        """
        if subscription is None:
            subscription = self.__defaultSubscriptionClass()
        if callable(getattr(subscription, "put_nowait", None)):
            self.__splog.debug("Added subscription %s", subscription)
            self.__subscriptions.add(subscription)
        elif inspect.iscoroutinefunction(subscription):
            self.__splog.debug("Added async callback %s", subscription)
            self.__cocallbacks.add(subscription)
        else:
            self.__splog.debug("Added callback %s", subscription)
            self.__callbacks.add(subscription)
        return subscription

    def _put_nowait(self, element):
        """
        Used by subclasses to add data to all subscriptions. This method internally
        calls all registered callbacks for you, so you only need to worry about
        the single function call.

        Warning:
            Only call this if you are subclassing :class:`BaseSubscriptionProducer`.
        """
        for s in self.__subscriptions:
            self.__splog.debug("put data into %s", s)
            s.put_nowait(element)
        for c in self.__callbacks:
            self.__splog.debug("calling %s", c)
            c(element)
        for c in self.__cocallbacks:
            self.__splog.debug("setting up future for %s", c)
            asyncio.ensure_future(c(element))

    def unsubscribe(self, subscription=None):
        """
        Removes the given subscription, so that it no longer gets updated::

            subs = myobj.subscribe()
            myobj.unsubscribe(subs)

        If no argument is given, removes the default subscription created by `get()`.
        If none exists, then does nothing.

        Args:
            subscription (optional):
                Anything that was passed into/returned from :func:`subscribe`.

        """
        if subscription is None:
            if self.__defaultSubscription is not None:
                self.__splog.debug("Removing default subscription")
                self.unsubscribe(self.__defaultSubscription)
                self.__defaultSubscription = None
            else:
                # Otherwise, do nothing
                self.__splog.debug(
                    "Unsubscribe called, but no default subscription is active. Doing nothing."
                )
        else:
            if callable(getattr(subscription, "put_nowait", None)):
                self.__splog.debug("Removing subscription %s", subscription)
                self.__subscriptions.remove(subscription)
            elif inspect.iscoroutinefunction(subscription):
                self.__splog.debug("Removing async callback %s", subscription)
                self.__cocallbacks.remove(subscription)
            else:
                self.__splog.debug("Removing callback %s", subscription)
                self.__callbacks.remove(subscription)

    def unsubscribeAll(self):
        """
        Removes all currently active subscriptions, including the default one if it was intialized.
        """
        self.__subscriptions = set()
        self.__callbacks = set()
        self.__cocallbacks = set()
        self.__defaultSubscription = None

    def __defaultSubscribe(self):
        if self.__defaultSubscription is None:
            self.__defaultSubscription = self.subscribe()
            self.__splog.debug(
                "Created default subscription %s", self.__defaultSubscription
            )

    async def get(self):
        """
        Behaves similarly to :func:`subscribe().get()`. On the first call, creates a default 
        subscription, and all subsequent calls to :func:`get()` use that subscription.

        If :func:`unsubscribe` is called, the subscription is deleted, so a subsequent call to :func:`get`
        will create a new one::

            data = await myobj.get() # Creates subscription on first call
            data = await myobj.get() # Same subscription
            myobj.unsubscribe()
            data2 = await myobj.get() # A new subscription

        The above code is equivalent to the following::

            defaultSubscription = myobj.subscribe()
            data = await defaultSubscription.get()
            data = await defaultSubscription.get()
            myobj.unsubscribe(defaultSubscription)
            newDefaultSubscription = myobj.subscribe()
            data = await newDefaultSubscription.get()
        """
        self.__defaultSubscribe()

        return await self.__defaultSubscription.get()

    def close(self):
        """
        Shuts down the data gathering, and removes all subscriptions.
        """
        self.__splog.debug("Closing")
        self._shouldClose = True
        self.unsubscribeAll()
        self._ready = False

    @property
    def ready(self):
        """
        This is :code:`True` when the class has been fully initialized. It becomes `False` when the class has been closed. 
        You usually don't need to use this,
        since :func:`subscribe` will work even if the class is still starting up in the background.
        """
        return self._ready  # No need to lock, as this thread only reads a binary T/F


class BaseSubscriptionConsumer:
    """
    A base class upon which consumers of subscriptions can be built. 

    The BaseSubscriptionConsumer class handles the logic of switching incoming subscriptions mid-stream and
    all the other annoying stuff.
    """

    def __init__(
        self, directPutSubscriptionType=asyncio.Queue, logger=None, ready=True
    ):

        self.__directPutSubscriptionType = directPutSubscriptionType
        self.__directPutSubscription = directPutSubscriptionType()
        self._subscription = self.__directPutSubscription
        self._shouldClose = False

        # The task used for getting data in _get. This allows us to cancel the task, and switch out subscriptions
        # at any point in time!
        self._getTask = None

        if logger is None:
            self.__sclog = logging.getLogger(self.__class__.__name__).getChild(
                "SubscriptionConsumer"
            )
        else:
            self.__sclog = logger.getChild("SubscriptionConsumer")

        # Once all init is finished, need to set self._ready to True
        self._ready = ready

    async def _get(self):
        """
        Warning:
            Only call this if you are subclassing :class:`BaseSubscriptionConsumer`.

        This function is to be awaited by a subclass to get the next datapoint 
        from the active subscription. It internally handles the subscription for you,
        and transparently manages the user switching a subscription during runtime::

            myobj.putSubscription(x)
            #  await self._get() waits on next datapoint from x
            myobj.putSubscription(y)
            # _get transparently switched to waiting on y

        Raises:
            :class:`SubscriptionClosed`:
                If :func:`close` was called, this error is raised, signalling your 
                data processing function to clean up and exit.

        Returns:
            The next datapoint that was put or subscribed to from the currently active
            subscription.

        
        """
        while not self._shouldClose:
            self._getTask = asyncio.create_task(self._subscription.get())

            try:
                self.__sclog.debug("Waiting for new data...")
                await self._getTask
                return self._getTask.result()
            except asyncio.CancelledError:
                # If the coroutine was cancelled, it means that self._subscription was replaced,
                # so we just loop back to await the new one
                self.__sclog.debug("Subscription cancelled  - checking for new tasks")
            except SubscriptionClosed:
                self.__sclog.debug(
                    "Incoming subscription closed - checking for new subscription"
                )
            except:
                self.__sclog.exception("Got unrecognized error from task. ignoring:")

        self.__sclog.debug("close() was called. raising SubscriptionClosed.")
        raise SubscriptionClosed("SubscriptionConsumer has been closed")

    def put_nowait(self, data):
        """
        Direct API for sending data to the reader, without needing to pass a subscription.
        """
        if self._subscription != self.__directPutSubscription:
            # If the subscription is not the default, stop, which will create a new default,
            # to which we can add our data
            self.stop()
        self.__sclog.debug(
            "put data with subscription %s", self.__directPutSubscription
        )
        self.__directPutSubscription.put_nowait(data)

    def putSubscription(self, subscription):
        """
        Given a subscription, such that `await subscription.get()` returns successive pieces of data,
        keeps reading the subscription until it is replaced.
        Equivalent to doing the following in the background::

            while True:
                sr.put_nowait(await subscription.get())
        """
        if subscription == self._subscription:
            return
        self.__sclog.debug(
            "Changing subscription from %s to %s", self._subscription, subscription
        )
        self._subscription = subscription
        if self._getTask is not None and not self._getTask.done():
            self.__sclog.debug("Canceling currently running subscription")
            self._getTask.cancel()

    def stop(self):
        """
        Stops reading the current subscription. Forgets any subscription,
        and waits for new data, which is passed through `put_nowait` or `readSubscription`
        """
        self.__directPutSubscription = self.__directPutSubscriptionType()
        self.putSubscription(
            self.__directPutSubscription
        )  # read the empty subscription

    def close(self):
        """
        Cleans up and closes the object.
        """
        self.__sclog.debug("Closing")
        self._ready = False
        self._shouldClose = True
        if self._getTask is not None and not self._getTask.done():
            self._getTask.cancel()

    @property
    def subscription(self):
        """
        Returns the currently active subscription. 
        If no subscription is active, you can still use func:`put_nowait` to add new data.
        """
        if self._subscription == self.__directPutSubscription:
            return None
        return self._subscription

    @property
    def ready(self):
        """
        This is `True` when the class has been fully initialized. You usually don't need to use this,
        since :func:`put_nowait` and func:`putSubscription` will work even if the class is still starting up in the background.
        """
        return self._ready  # No need to lock, as this thread only reads a binary T/F


class ThreadedSubscriptionProducer(BaseSubscriptionProducer):
    def __init__(
        self,
        defaultSubscriptionType=asyncio.Queue,
        logger=None,
        loop=None,
        daemonThread=True,
    ):
        super().__init__(defaultSubscriptionType, logger=logger, ready=False)

        self._loop = loop
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

        self._producerThread = threading.Thread(target=self._producer)
        self._producerThread.daemon = daemonThread
        self._producerThread.start()

    def _put_nowait(self, data):
        """
        To be called by the producer thread to insert data.

        """
        self._loop.call_soon_threadsafe(super()._put_nowait, data)

    def _producer(self):
        """
        This is the function run in another thread. You override the function with your own logic.

        The base implementation is used for testing
        """
        import queue

        self.testQueue = queue.Queue()
        self.testResultQueue = queue.Queue()

        # We are ready!
        self._ready = True
        while not self._shouldClose:
            # In real code, there should be a timeout in get to make sure _shouldClose is not True
            try:
                self._put_nowait(self.testQueue.get(1))
            except TimeoutError:
                pass
        self.testResultQueue.put("<<END>>")

    def close(self):
        """
        Shuts down data gathering, and closes all subscriptions. Note that it is not recommended
        to call this in an async function, since it waits until the background thread joins.

        The object is meant to be used as a singleton, which is initialized at the start of your code,
        and is closed at the end.
        """
        super().close()
        self._producerThread.join()


class ProcessSubscriptionProducer(BaseSubscriptionProducer):
    def __init__(
        self,
        defaultSubscriptionType=asyncio.Queue,
        logger=None,
        loop=None,
        daemonProcess=True,
        joinTimeout=1,
    ):
        self._joinTimeout = joinTimeout
        if logger is None:
            self.__splog = logging.getLogger(self.__class__.__name__).getChild(
                "ProcessSubscriptionProducer"
            )
        else:
            self.__splog = logger.getChild("ProcessSubscriptionConsumer")

        self.__readyEvent = multiprocessing.Event()
        self.__closeEvent = multiprocessing.Event()

        super().__init__(defaultSubscriptionType, logger=logger, ready=False)

        self._loop = loop
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

        self._producerQueue = multiprocessing.Queue()

        self.__queueReaderThread = threading.Thread(target=self.__queueReader)
        self.__queueReaderThread.daemon = True
        self.__queueReaderThread.start()

        self._producerProcess = multiprocessing.Process(target=self.__producerSetup)
        self._producerProcess.daemon = daemonProcess
        self._producerProcess.start()

    @property
    def _shouldClose(self):
        # We need to check the event
        return self.__closeEvent.is_set()

    @_shouldClose.setter
    def _shouldClose(self, value):
        self.__splog.debug("Setting _shouldClose to %s", value)
        if value:
            self.__closeEvent.set()
        else:
            self.__closeEvent.clear()

    @property
    def _ready(self):
        # We need to check the event
        return self.__readyEvent.is_set()

    @_ready.setter
    def _ready(self, value):
        self.__splog.debug("setting _ready to %s", value)
        if value:
            self.__readyEvent.set()
        else:
            self.__readyEvent.clear()

    def __queueReader(self):
        while not self._shouldClose:
            try:
                data = self._producerQueue.get(timeout=self._joinTimeout)
                self.__splog.debug("Received data from remote process")
                self._loop.call_soon_threadsafe(super()._put_nowait, data)
            except queue.Empty:
                pass  # No need to notify each time we check whether we chould close

    def _put_nowait(self, data):
        """
        To be called by the producer thread to insert data.

        """
        self.__splog.debug("Sending data from remote process")
        self._producerQueue.put_nowait(data)

    def __producerSetup(self):
        # This function sets up the producer. In particular, it receives KeyboardInterrupts

        def handleInterrupt(sig, frame):
            self.__splog.warning("Received KeyboardInterrupt - not notifying process")

        old_handler = signal.signal(signal.SIGINT, handleInterrupt)
        try:
            self._producer()
        except:
            self.__splog.exception("The remote process had an exception!")
        self._ready = False
        self._shouldClose = True

        signal.signal(signal.SIGINT, old_handler)

        self.__splog.debug("Exiting remote process")

    def _producer(self):
        """
        This is the function run in another thread. You override the function with your own logic.

        The base implementation is used for testing
        """

        # We are ready!
        self._ready = True
        # Have to think how to make this work
        # in testing

    def close(self):
        """
        Shuts down data gathering, and closes all subscriptions. Note that it is not recommended
        to call this in an async function, since it waits until the background thread joins.

        The object is meant to be used as a singleton, which is initialized at the start of your code,
        and is closed at the end.
        """
        super().close()
        self._producerProcess.join(self._joinTimeout)
        self.__queueReaderThread.join()
        if self._producerProcess.is_alive():
            self.__splog.warning("Process did not terminate in time. Killing it.")
            self._producerProcess.terminate()
            self._producerProcess.join()


class ThreadedSubscriptionConsumer(BaseSubscriptionConsumer):
    def __init__(
        self,
        directPutSubscriptionType=asyncio.Queue,
        logger=None,
        loop=None,
        daemonThread=True,
    ):
        super().__init__(directPutSubscriptionType, logger=logger, ready=False)

        self._loop = loop
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

        if logger is None:
            self.__sclog = logging.getLogger(self.__class__.__name__).getChild(
                "ThreadedSubscriptionConsumer"
            )
        else:
            self.__sclog = logger.getChild("ThreadedSubscriptionConsumer")

        self._taskLock = threading.Lock()

        self._consumerThread = threading.Thread(target=self._consumer)
        self._consumerThread.daemon = daemonThread
        self._consumerThread.start()

    def _get(self):
        """
        This is not a coroutine - it is to be called in the worker thread.
        If the worker thread is to be shut down, raises a SubscriptionClosed exception.
        """
        while not self._shouldClose:
            with self._taskLock:
                self._getTask = asyncio.run_coroutine_threadsafe(
                    self._subscription.get(), self._loop
                )
            try:
                return self._getTask.result(1)
            except asyncio.CancelledError:
                self.__sclog.debug("Subscription cancelled - checking for new tasks")
            except asyncio.TimeoutError:
                self.__sclog.debug("No incoming data for 1 second...")
            except SubscriptionClosed:
                self.__sclog.debug(
                    "Incoming stream closed... Checking for new subscription"
                )
        self.__sclog.debug(
            "close() was called on the aio thread. raising SubscriptionClosed."
        )
        raise SubscriptionClosed("ThreadedSubscriptionConsumer has been closed")

    def _consumer(self):
        """
        This is the function that is to be overloaded by the superclass to read data.
        It is run in a separate thread. It should call self._get() to get the next datapoint coming
        from a subscription.

        The default implementation is used for testing
        """

        import queue

        self.testQueue = queue.Queue()

        # We are ready!
        self._ready = True
        try:
            while True:
                data = self._get()
                self.testQueue.put(data)
        except SubscriptionClosed:
            self.testQueue.put("<<END>>")

    def putSubscription(self, subscription):
        with self._taskLock:
            super().putSubscription(subscription)

    def close(self):
        """
        Closes the object. Note that it is not recommended
        to call this in an async function, since it waits until the background thread joins.

        The object is meant to be used as a singleton, which is initialized at the start of your code,
        and is closed at the end.
        """
        with self._taskLock:
            super().close()
        self._consumerThread.join()


class SubscriptionProducer(BaseSubscriptionProducer):
    def put_nowait(self, element):
        self._put_nowait(element)


class SubscriptionConsumer(BaseSubscriptionConsumer):
    async def get(self):
        return await self._get()


class SubscriptionProducerConsumer(BaseSubscriptionConsumer, BaseSubscriptionProducer):
    """
    This base class represents an object which is both a producer and consumer. This is common
    with two-way connections.

    Here, you call _get() to consume the incoming data, and _put_nowait() to produce outgoing data.
    """

    def __init__(
        self,
        directPutSubscriptionType=asyncio.Queue,
        defaultSubscriptionType=asyncio.Queue,
        logger=None,
        ready=False,
        defaultAutosubscribe=False,
    ):
        BaseSubscriptionConsumer.__init__(
            self, directPutSubscriptionType, logger=logger, ready=ready
        )
        BaseSubscriptionProducer.__init__(
            self,
            defaultSubscriptionType,
            logger=logger,
            ready=ready,
            defaultAutosubscribe=defaultAutosubscribe,
        )

    def close(self):
        BaseSubscriptionConsumer.close(self)
        BaseSubscriptionProducer.close(self)

