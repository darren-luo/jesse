from abc import ABC, abstractmethod
from typing import List

import numpy as np
import pydash
from time import sleep

import jesse.helpers as jh
import jesse.services.logger as logger
import jesse.services.selectors as selectors
from jesse.enums import sides, trade_types, order_roles
from jesse import exceptions
from jesse.models import CompletedTrade, Order
from jesse.services.broker import Broker
from jesse.store import store


class Strategy(ABC):
    """The parent strategy class which every strategy must extend"""

    def __init__(self):
        self.id = jh.generate_unique_id()
        self.name = None
        self.symbol = None
        self.exchange = None
        self.timeframe = None
        self.hp = None

        self.index = 0
        self.vars = {}

        self.buy = None
        self._buy = None
        self.sell = None
        self._sell = None
        self.stop_loss = None
        self._stop_loss = None
        self.take_profit = None
        self._take_profit = None
        self._log_take_profit = None
        self._log_stop_loss = None

        self._open_position_orders = []
        self._stop_loss_orders = []
        self._take_profit_orders = []

        self.trade = None
        self.trades_count = 0

        self._initial_qty = None
        self._is_executing = False
        self._is_initiated = False

        self.position = None
        self.broker = None

    def _init_objects(self):
        """
        This method gets called after right creating the Strategy object. It
        is just a workaround as a part of not being able to set them inside
        self.__init__() for the purpose of removing __init__() methods from strategies.
        """
        self.position = selectors.get_position(self.exchange, self.symbol)
        self.broker = Broker(self.position, self.exchange, self.symbol, self.timeframe)

    @property
    def is_reduced(self):
        """
        Has the size of position been reduced since it was opened
        :return: bool
        """
        if self.position.is_close:
            return None

        return self.position.qty < self._initial_qty

    @property
    def is_increased(self):
        if self.position.is_close:
            return None

        return self.position.qty > self._initial_qty

    def _broadcast(self, msg: str):
        """Broadcasts the event to all OTHER strategies

        Arguments:
            msg {str} -- [the message to broadcast]
        """
        from jesse.routes import router

        for r in router.routes:
            # skip self
            if r.strategy.id == self.id:
                continue

            if msg == 'route-open-position':
                r.strategy.on_route_open_position(self)
            elif msg == 'route-stop-loss':
                r.strategy.on_route_stop_loss(self)
            elif msg == 'route-take-profit':
                r.strategy.on_route_take_profit(self)
            elif msg == 'route-increased-position':
                r.strategy.on_route_increased_position(self)
            elif msg == 'route-reduced-position':
                r.strategy.on_route_reduced_position(self)
            elif msg == 'route-canceled':
                r.strategy.on_route_canceled(self)

            r.strategy._detect_and_handle_entry_and_exit_modifications()

    def _on_updated_position(self, order: Order):
        """handles executed order
        Note that it assumes that the position has already been affected
        by the executed order.

        Arguments:
            order {Order} -- the executed order object
        """
        role = order.role

        if role == order_roles.OPEN_POSITION and abs(self.position.qty) != abs(order.qty):
            order.role = order_roles.INCREASE_POSITION
            role = order_roles.INCREASE_POSITION

        if role == order_roles.CLOSE_POSITION and self.position.is_open:
            order.role = order_roles.REDUCE_POSITION
            role = order_roles.REDUCE_POSITION

        self._log_position_update(order, role)

        if role == order_roles.OPEN_POSITION:
            self._on_open_position()
        elif role == order_roles.CLOSE_POSITION and order in self._take_profit_orders:
            self._on_take_profit()
        elif role == order_roles.CLOSE_POSITION and order in self._stop_loss_orders:
            self._on_stop_loss()
        elif role == order_roles.INCREASE_POSITION:
            self._on_increased_position()
        elif role == order_roles.REDUCE_POSITION:
            self._on_reduced_position()

    def filters(self):
        return []

    @staticmethod
    def hyper_parameters():
        return []

    def _execute_long(self):
        self.go_long()

        # validation
        if self.buy is None:
            raise exceptions.InvalidStrategy('You forgot to set self.buy. example [qty, price]')
        elif type(self.buy) not in [tuple, list]:
            raise exceptions.InvalidStrategy('self.buy must be either a list or a tuple. example: [qty, price]')

        self._prepare_buy()

        if self.take_profit is not None:
            # validate
            self._validate_take_profit()

            self._prepare_take_profit()

        if self.stop_loss is not None:
            # validate
            self._validate_stop_loss()

            self._prepare_stop_loss()

        # filters
        for f in self.filters():
            passed = f()
            if passed == False:
                logger.info(f.__name__)
                self._reset()
                return

        for o in self._buy:
            # STOP order
            if o[1] > self.price:
                self._open_position_orders.append(
                    self.broker.start_profit_at(sides.BUY, o[0], o[1], order_roles.OPEN_POSITION)
                )
            # LIMIT order
            elif o[1] < self.price:
                self._open_position_orders.append(
                    self.broker.buy_at(o[0], o[1], order_roles.OPEN_POSITION)
                )
            # MARKET order
            elif o[1] == self.price:
                self._open_position_orders.append(
                    self.broker.buy_at_market(o[0], order_roles.OPEN_POSITION)
                )

    def _prepare_buy(self, make_copies=True):
        # create a copy in the placeholders variables so we can detect future modifications
        # also, make it list of orders even if there's only one, to make it easier to loop
        if type(self.buy[0]) not in [list, tuple]:
            self.buy = [self.buy]
        self.buy = self._convert_to_numpy_array(self.buy, 'self.buy')

        if make_copies:
            self._buy = self.buy.copy()

    def _prepare_sell(self, make_copies=True):
        # create a copy in the placeholders variables so we can detect future modifications
        # also, make it list of orders even if there's only one, to make it easier to loop
        if type(self.sell[0]) not in [list, tuple]:
            self.sell = [self.sell]
        self.sell = self._convert_to_numpy_array(self.sell, 'self.sell')

        if make_copies:
            self._sell = self.sell.copy()

    def _prepare_stop_loss(self, make_copies=True):
        # if it's numpy, then it has already been prepared
        if type(self.stop_loss) is np.ndarray:
            return

        if type(self.stop_loss[0]) not in [list, tuple, np.ndarray]:
            self.stop_loss = [self.stop_loss]
        self.stop_loss = self._convert_to_numpy_array(self.stop_loss, 'self.stop_loss')

        if make_copies:
            self._stop_loss = self.stop_loss.copy()
            self._log_stop_loss = self._stop_loss.copy()

    def _prepare_take_profit(self, make_copies=True):
        # if it's numpy, then it has already been prepared
        if type(self.take_profit) is np.ndarray:
            return

        if type(self.take_profit[0]) not in [list, tuple, np.ndarray]:
            self.take_profit = [self.take_profit]
        self.take_profit = self._convert_to_numpy_array(self.take_profit, 'self.take_profit')

        if make_copies:
            self._take_profit = self.take_profit.copy()
            self._log_take_profit = self._take_profit.copy()

    @staticmethod
    def _convert_to_numpy_array(arr, name):
        if type(arr) is np.ndarray:
            return arr

        try:
            # create numpy array from list
            arr = np.array(arr, dtype=float)

            if jh.is_live():
                # in livetrade mode, we'll need them rounded
                price = arr[0][1]

                prices = jh.round_price_for_live_mode(price, arr[:, 1])
                qtys = jh.round_qty_for_live_mode(price, arr[:, 0])

                arr[:, 0] = qtys
                arr[:, 1] = prices

            return arr
        except ValueError:
            raise exceptions.InvalidShape(
                'The format of {} is invalid. \n'
                'It must be (qty, price) or [(qty, price), (qty, price)] for multiple points; but {} was given'.format(
                    name, arr
                )
            )

    def _validate_stop_loss(self):
        if self.stop_loss is None:
            raise exceptions.InvalidStrategy('You forgot to set self.stop_loss. example [qty, price]')
        elif type(self.stop_loss) not in [tuple, list, np.ndarray]:
            raise exceptions.InvalidStrategy('self.stop_loss must be either a list or a tuple. example: [qty, price]')

    def _validate_take_profit(self):
        if self.take_profit is None:
            raise exceptions.InvalidStrategy('You forgot to set self.take_profit. example [qty, price]')
        elif type(self.take_profit) not in [tuple, list, np.ndarray]:
            raise exceptions.InvalidStrategy('self.take_profit must be either a list or a tuple. example: [qty, price]')

    def _execute_short(self):
        self.go_short()

        # validation
        if self.sell is None:
            raise exceptions.InvalidStrategy('You forgot to set self.sell. example [qty, price]')
        elif type(self.sell) not in [tuple, list]:
            raise exceptions.InvalidStrategy('self.sell must be either a list or a tuple. example: [qty, price]')

        self._prepare_sell()

        if self.take_profit is not None:
            self._validate_take_profit()
            self._prepare_take_profit()

        if self.stop_loss is not None:
            self._validate_stop_loss()
            self._prepare_stop_loss()

        for f in self.filters():
            passed = f()
            if passed is False:
                logger.info(f.__name__)
                self._reset()
                return

        for o in self._sell:
            # STOP order
            if o[1] < self.price:
                self._open_position_orders.append(
                    self.broker.start_profit_at(sides.SELL, o[0], o[1], order_roles.OPEN_POSITION)
                )
            # LIMIT order
            elif o[1] > self.price:
                self._open_position_orders.append(
                    self.broker.sell_at(o[0], o[1], order_roles.OPEN_POSITION)
                )
            # MARKET order
            elif o[1] == self.price:
                self._open_position_orders.append(
                    self.broker.sell_at_market(o[0], order_roles.OPEN_POSITION)
                )

    @abstractmethod
    def go_long(self):
        pass

    @abstractmethod
    def go_short(self):
        pass

    def _execute_cancel(self):
        """
        cancels everything so that the strategy can keep looking for new trades.
        """
        # validation
        if self.position.is_open:
            raise Exception('cannot cancel orders when position is still open. there must be a bug somewhere.')

        logger.info('cancel all remaining orders to prepare for a fresh start...')

        self.broker.cancel_all_orders()

        self._reset()

        self._broadcast('route-canceled')

        self.on_cancel()

        if not jh.is_unit_testing() and not jh.is_live():
            store.orders.storage['{}-{}'.format(self.exchange, self.symbol)].clear()

    def _reset(self):
        self.buy = None
        self._buy = None
        self.sell = None
        self._sell = None
        self.stop_loss = None
        self._stop_loss = None
        self.take_profit = None
        self._take_profit = None
        self._log_take_profit = None
        self._log_stop_loss = None

        self._open_position_orders = []
        self._stop_loss_orders = []
        self._take_profit_orders = []

        self._initial_qty = None

    def on_cancel(self):
        """
        what should happen after all active orders have been cancelled
        """
        pass

    @abstractmethod
    def should_long(self) -> bool:
        """are all filters good to execute buy"""
        pass

    @abstractmethod
    def should_short(self) -> bool:
        """are all filters good to execute sell"""
        pass

    @abstractmethod
    def should_cancel(self) -> bool:
        pass

    def prepare(self):
        """What should get updated after each strategy execution?"""
        pass

    def _update_position(self):
        self.update_position()

        self._detect_and_handle_entry_and_exit_modifications()

    def _detect_and_handle_entry_and_exit_modifications(self):
        if self.position.is_close:
            return

        if self.is_long:
            # prepare format
            if type(self.buy[0]) not in [list, tuple, np.ndarray]:
                self.buy = [self.buy]
            self.buy = np.array(self.buy, dtype=float)

            # if entry has been modified
            if not np.array_equal(self.buy, self._buy):
                self._buy = self.buy.copy()

                # cancel orders
                for o in self._open_position_orders:
                    if o.is_active or o.is_queued:
                        self.broker.cancel_order(o.id)

                # clean orders array but leave executed ones
                self._open_position_orders = [o for o in self._open_position_orders if o.is_executed]
                for o in self._buy:
                    # STOP order
                    if o[1] > self.price:
                        self._open_position_orders.append(
                            self.broker.start_profit_at(sides.BUY, o[0], o[1], order_roles.OPEN_POSITION)
                        )
                    # LIMIT order
                    elif o[1] < self.price:
                        self._open_position_orders.append(
                            self.broker.buy_at(o[0], o[1], order_roles.OPEN_POSITION)
                        )
                    # MARKET order
                    elif o[1] == self.price:
                        self._open_position_orders.append(
                            self.broker.buy_at_market(o[0], order_roles.OPEN_POSITION)
                        )

        elif self.is_short:
            # prepare format
            if type(self.sell[0]) not in [list, tuple, np.ndarray]:
                self.sell = [self.sell]
            self.sell = np.array(self.sell, dtype=float)

            # if entry has been modified
            if not np.array_equal(self.sell, self._sell):
                self._sell = self.sell.copy()

                # cancel orders
                for o in self._open_position_orders:
                    if o.is_active or o.is_queued:
                        self.broker.cancel_order(o.id)

                # clean orders array but leave executed ones
                self._open_position_orders = [o for o in self._open_position_orders if o.is_executed]

                for o in self._sell:
                    # STOP order
                    if o[1] > self.price:
                        self._open_position_orders.append(
                            self.broker.start_profit_at(sides.BUY, o[0], o[1], order_roles.OPEN_POSITION)
                        )
                    # LIMIT order
                    elif o[1] < self.price:
                        self._open_position_orders.append(
                            self.broker.sell_at(o[0], o[1], order_roles.OPEN_POSITION)
                        )
                    # MARKET order
                    elif o[1] == self.price:
                        self._open_position_orders.append(
                            self.broker.sell_at_market(o[0], order_roles.OPEN_POSITION)
                        )

        if self.position.is_open and self.take_profit is not None:
            self._validate_take_profit()
            self._prepare_take_profit(False)

            # if _take_profit has been modified
            if not np.array_equal(self.take_profit, self._take_profit):
                self._take_profit = self.take_profit.copy()

                # cancel orders
                for o in self._take_profit_orders:
                    if o.is_active or o.is_queued:
                        self.broker.cancel_order(o.id)

                # clean orders array but leave executed ones
                self._take_profit_orders = [o for o in self._take_profit_orders if o.is_executed]
                self._log_take_profit = []
                for s in self._take_profit_orders:
                    self._log_take_profit.append(
                        (abs(s.qty), s.price)
                    )
                for o in self._take_profit:
                    self._log_take_profit.append(o)

                    if o[1] == self.price:
                        if self.is_long:
                            self._take_profit_orders.append(
                                self.broker.sell_at_market(o[0], role=order_roles.CLOSE_POSITION)
                            )
                        elif self.is_short:
                            self._take_profit_orders.append(
                                self.broker.buy_at_market(o[0], role=order_roles.CLOSE_POSITION)
                            )
                    else:
                        if (self.is_long and o[1] > self.price) or (self.is_short and o[1] < self.price):

                            self._take_profit_orders.append(
                                self.broker.reduce_position_at(
                                    o[0],
                                    o[1],
                                    order_roles.CLOSE_POSITION
                                )
                            )
                        elif (self.is_long and o[1] < self.price) or (self.is_short and o[1] > self.price):
                            self._take_profit_orders.append(
                                self.broker.stop_loss_at(
                                    o[0],
                                    o[1],
                                    order_roles.CLOSE_POSITION
                                )
                            )

        if self.position.is_open and self.stop_loss is not None:
            self._validate_stop_loss()
            self._prepare_stop_loss(False)

            # if stop_loss has been modified
            if not np.array_equal(self.stop_loss, self._stop_loss):
                # prepare format
                self._stop_loss = self.stop_loss.copy()

                # cancel orders
                for o in self._stop_loss_orders:
                    if o.is_active or o.is_queued:
                        self.broker.cancel_order(o.id)

                # clean orders array but leave executed ones
                self._stop_loss_orders = [o for o in self._stop_loss_orders if o.is_executed]
                self._log_stop_loss = []
                for s in self._stop_loss_orders:
                    self._log_stop_loss.append(
                        (abs(s.qty), s.price)
                    )
                for o in self._stop_loss:
                    self._log_stop_loss.append(o)

                    if o[1] == self.price:
                        if self.is_long:
                            self._stop_loss_orders.append(
                                self.broker.sell_at_market(o[0], role=order_roles.CLOSE_POSITION)
                            )
                        elif self.is_short:
                            self._stop_loss_orders.append(
                                self.broker.buy_at_market(o[0], role=order_roles.CLOSE_POSITION)
                            )
                    else:
                        self._stop_loss_orders.append(
                            self.broker.stop_loss_at(
                                o[0],
                                o[1],
                                order_roles.CLOSE_POSITION
                            )
                        )

        # validations: stop-loss and take-profit should not be the same
        if self.position.is_open:
            if (self.stop_loss is not None and self.take_profit is not None) and np.array_equal(self.stop_loss, self.take_profit):
                raise exceptions.InvalidStrategy('stop-loss and take-profit should not be exactly the same. Just use either one of them and it will do.')

    def update_position(self):
        pass

    def _check(self):
        """Based on the newly updated info, check if we should take action or not"""
        if not self._is_initiated:
            self._is_initiated = True

        if jh.is_live() and jh.is_debugging():
            logger.info('Executing  {}-{}-{}-{}'.format(self.name, self.exchange, self.symbol, self.timeframe))

        # for caution to make sure testing on livetrade won't bleed your account
        if jh.is_test_driving() and store.completed_trades.count >= 2:
            logger.info('Maximum allowed trades in test-drive mode is reached')
            return

        if self._open_position_orders != [] and self.should_cancel():
            self._execute_cancel()

            # make sure order cancellation response is received via WS
            if jh.is_live():
                # sleep a little until cancel is received via WS
                sleep(0.1)
                # just in case, sleep some more if necessary
                for _ in range(20):
                    if store.orders.count_active_orders(self.exchange, self.symbol) == 0:
                        break

                    logger.info('sleeping 0.2 more seconds...')
                    sleep(0.2)

                # If it's still not cancelled, something is wrong. Handle cancellation failure
                if store.orders.count_active_orders(self.exchange, self.symbol) != 0:
                    raise exceptions.ExchangeNotResponding(
                        'The exchange did not respond as expected'
                    )

        if self.position.is_open:
            self._update_position()

        if jh.is_backtesting() or jh.is_unit_testing():
            store.orders.execute_pending_market_orders()

        if self.position.is_close and self._open_position_orders == []:
            # validation
            if self.should_short() and self.should_long():
                raise exceptions.ConflictingRules(
                    'should_short and should_long should not be true at the same time.'
                )

            if self.should_long():
                self._execute_long()

            if self.should_short():
                self._execute_short()

    def _on_open_position(self):
        logger.info('Detected open position')
        self._broadcast('route-open-position')

        if self.take_profit is not None:
            for o in self._take_profit:
                # validation: make sure take-profit will exit with profit
                if self.is_long:
                    if o[1] <= self.position.entry_price:
                        raise exceptions.InvalidStrategy(
                            'take-profit({}) must be above entry-price({}) in a long position'.format(
                                o[1],
                                self.position.entry_price
                            )
                        )
                elif self.is_short:
                    if o[1] >= self.position.entry_price:
                        raise exceptions.InvalidStrategy(
                            'take-profit({}) must be below entry-price({}) in a short position'.format(
                                o[1],
                                self.position.entry_price
                            )
                        )

                # submit take-profit
                self._take_profit_orders.append(
                    self.broker.reduce_position_at(
                        o[0],
                        o[1],
                        order_roles.CLOSE_POSITION
                    )
                )

        if self.stop_loss is not None:
            for o in self._stop_loss:
                # validation
                if self.is_long:
                    if o[1] >= self.position.entry_price:
                        raise exceptions.InvalidStrategy(
                            'stop-loss({}) must be below entry-price({}) in a long position'.format(
                                o[1],
                                self.position.entry_price
                            )
                        )
                elif self.is_short:
                    if o[1] <= self.position.entry_price:
                        raise exceptions.InvalidStrategy(
                            'stop-loss({}) must be above entry-price({}) in a short position'.format(
                                o[1],
                                self.position.entry_price
                            )
                        )

                # submit stop-loss
                self._stop_loss_orders.append(
                    self.broker.stop_loss_at(
                        o[0],
                        o[1],
                        order_roles.CLOSE_POSITION
                    )
                )

        self._open_position_orders = []
        self._initial_qty = self.position.qty
        self.on_open_position()
        self._detect_and_handle_entry_and_exit_modifications()

    def on_open_position(self):
        """
        What should happen after the open position order has been executed
        """
        pass

    def _on_stop_loss(self):
        if not jh.should_execute_silently() or jh.is_debugging():
            logger.info('Yikes! stop-loss has been executed.')

        self._broadcast('route-stop-loss')
        self._execute_cancel()
        self.on_stop_loss()

        self._detect_and_handle_entry_and_exit_modifications()

    def on_stop_loss(self):
        """
        What should happen after the stop-loss order has been executed
        """
        pass

    def _on_take_profit(self):
        if not jh.should_execute_silently() or jh.is_debugging():
            logger.info("Sweet! Take profit order has been executed.")

        self._broadcast('route-take-profit')
        self._execute_cancel()
        self.on_take_profit()

        self._detect_and_handle_entry_and_exit_modifications()

    def on_take_profit(self):
        """
        What should happen after the take-profit order is executed.
        """
        pass

    def _on_increased_position(self):
        if not jh.should_execute_silently() or jh.is_debugging():
            logger.info("Position size increased.")

        self._open_position_orders = []

        self._broadcast('route-increased-position')

        self.on_increased_position()

        self._detect_and_handle_entry_and_exit_modifications()

    def on_increased_position(self):
        """
        What should happen after the order (if any) increasing the
        size of the position is executed. Overwrite it if needed.
        And leave it be if your strategy doesn't require it
        """
        pass

    def _on_reduced_position(self):
        """
        prepares for on_reduced_position() is implemented by user
        """
        if not jh.should_execute_silently() or jh.is_debugging():
            logger.info("Position size reduced.")

        self._open_position_orders = []

        self._broadcast('route-reduced-position')

        self.on_reduced_position()

        self._detect_and_handle_entry_and_exit_modifications()

    def on_reduced_position(self):
        """
        What should happen after the order (if any) reducing the size of the position is executed.
        """
        pass

    def on_route_open_position(self, strategy):
        """used when trading multiple routes that related

        Arguments:
            strategy {Strategy} -- the strategy that has fired (and not listening to) the event
        """
        pass

    def on_route_stop_loss(self, strategy):
        """used when trading multiple routes that related
        """
        pass

    def on_route_take_profit(self, strategy):
        """used when trading multiple routes that related

        Arguments:
            strategy {Strategy} -- the strategy that has fired (and not listening to) the event
        """
        pass

    def on_route_increased_position(self, strategy):
        """used when trading multiple routes that related

        Arguments:
            strategy {Strategy} -- the strategy that has fired (and not listening to) the event
        """
        pass

    def on_route_reduced_position(self, strategy):
        """used when trading multiple routes that related

        Arguments:
            strategy {Strategy} -- the strategy that has fired (and not listening to) the event
        """
        pass

    def on_route_canceled(self, strategy):
        """used when trading multiple routes that related

        Arguments:
            strategy {Strategy} -- the strategy that has fired (and not listening to) the event
        """
        pass

    def _execute(self):
        """
        Handles the execution permission for the strategy.
        """
        # make sure we don't execute this strategy more than once at the same time.
        if self._is_executing is True:
            return

        self._is_executing = True

        self.prepare()
        self._check()

        self._is_executing = False
        self.index += 1

    def _terminate(self):
        """
        Optional for executing code after completion of a backTest.
        This block will not execute in live use as a live
        Jesse is never ending.
        """
        if not jh.should_execute_silently() or jh.is_debugging():
            logger.info("Terminating strategy...")

        self.terminate()

        self._detect_and_handle_entry_and_exit_modifications()

        # fake execution of market orders in backtest simulation
        if not jh.is_live():
            store.orders.execute_pending_market_orders()

        if jh.is_live():
            return

        if self.position.is_open:
            store.app.total_open_trades += 1
            store.app.total_open_pl += self.position.pnl
            logger.info(
                "Closed open {}-{} position at {} with PNL: {}({}%) because we reached the end of the backtest session.".format(
                    self.exchange, self.symbol, self.position.current_price, self.position.pnl,
                    self.position.pnl_percentage
                )
            )
            self.position._close(self.position.current_price)
            self._execute_cancel()
            return

        if self._open_position_orders:
            self._execute_cancel()
            logger.info('Canceled open-position orders because we reached the end of the backtest session.')

    def terminate(self):
        pass

    def watch_list(self):
        """
        returns an array containing an array of key-value items that should
        be logged when backTested, and monitored while liveTraded

        Returns:
            [array[{"key": v, "value": v}]] -- an array of dictionary objects
        """
        return []

    @property
    def current_candle(self) -> np.ndarray:
        """
        Returns current trading candle

        :return: np.ndarray
        """
        return store.candles.get_current_candle(self.exchange, self.symbol, self.timeframe).copy()

    @property
    def open(self):
        """
        Returns the closing price of the current candle for this strategy.
        Just as a helper to use when writing super simple strategies.
        Returns:
            [float] -- the current trading candle's OPEN price
        """
        return self.current_candle[1]

    @property
    def close(self):
        """
        Returns the closing price of the current candle for this strategy.
        Just as a helper to use when writing super simple strategies.
        Returns:
            [float] -- the current trading candle's CLOSE price
        """
        return self.current_candle[2]

    @property
    def price(self):
        """
        Same as self.close, except in livetrde, this is rounded as the exchanges require it.

        Returns:
            [float] -- the current trading candle's current(close) price
        """
        return self.position.current_price

    @property
    def high(self):
        """
        Returns the closing price of the current candle for this strategy.
        Just as a helper to use when writing super simple strategies.
        Returns:
            [float] -- the current trading candle's HIGH price
        """
        return self.current_candle[3]

    @property
    def low(self):
        """
        Returns the closing price of the current candle for this strategy.
        Just as a helper to use when writing super simple strategies.
        Returns:
            [float] -- the current trading candle's LOW price
        """
        return self.current_candle[4]

    @property
    def candles(self) -> np.ndarray:
        """
        Returns candles for current trading route

        :return: np.ndarray
        """
        return store.candles.get_candles(self.exchange, self.symbol, self.timeframe)

    def get_candles(self, exchange: str, symbol: str, timeframe: str) -> np.ndarray:
        """
        Get candles by passing exchange, symbol, and timeframe

        :param exchange: str
        :param symbol: str
        :param timeframe: str

        :return: np.ndarray
        """
        return store.candles.get_candles(exchange, symbol, timeframe)

    @property
    def orders(self) -> List[Order]:
        """
        Returns all the orders submitted by for this strategy. Just as a helper
        to use when writing super simple strategies.

        Returns:
            [List[Order]] -- orders submitted by strategy
        """
        return store.orders.get_orders(self.exchange, self.symbol)

    @property
    def time(self):
        """returns the current time"""
        return store.app.time

    @property
    def BTCUSD(self):
        """shortcut for BTCUSD symbol string """
        return 'BTCUSD' if self.exchange == 'Bitfinex' else 'BTCUSDT'

    @property
    def balance(self):
        """alias for self.capital"""
        return self.capital

    @property
    def capital(self):
        """the current capital in the trading exchange"""
        return selectors.get_exchange(self.exchange).balance

    def _log_position_update(self, order: Order, role: str):
        """
        A log can be either about opening, adding, reducing, or closing the position.

        Arguments:
            order {order} -- the order object
        """
        if role == order_roles.OPEN_POSITION:
            self.trade = CompletedTrade()
            self.trade.orders = [order]
            self.trade.timeframe = self.timeframe
            self.trade.id = order.id
            self.trade.strategy_name = self.name
            self.trade.exchange = order.exchange
            self.trade.symbol = order.symbol
            self.trade.type = trade_types.LONG if order.side == sides.BUY else trade_types.SHORT
            self.trade.qty = order.qty
            self.trade.opened_at = jh.now()
            self.trade.entry_candle_timestamp = self.current_candle[0]
        elif role == order_roles.INCREASE_POSITION:
            self.trade.orders.append(order)
            self.trade.qty += order.qty
        elif role == order_roles.REDUCE_POSITION:
            self.trade.orders.append(order)
            self.trade.qty += order.qty
        elif role == order_roles.CLOSE_POSITION:
            self.trade.exit_candle_timestamp = self.current_candle[0]
            self.trade.orders.append(order)

            # calculate average stop-loss price
            sum_price = 0
            sum_qty = 0
            if self._log_stop_loss is not None:
                for l in self._log_stop_loss:
                    sum_qty += abs(l[0])
                    sum_price += abs(l[0]) * l[1]
                self.trade.stop_loss_at = sum_price / sum_qty
            else:
                self.trade.stop_loss_at = np.nan

            # calculate average take-profit price
            sum_price = 0
            sum_qty = 0
            if self._log_take_profit is not None:
                for l in self._log_take_profit:
                    sum_qty += abs(l[0])
                    sum_price += abs(l[0]) * l[1]
                self.trade.take_profit_at = sum_price / sum_qty
            else:
                self.trade.take_profit_at = np.nan

            # calculate average entry_price price
            sum_price = 0
            sum_qty = 0
            for l in self.trade.orders:
                if not l.is_executed:
                    continue

                if jh.side_to_type(l.side) != self.trade.type:
                    continue

                sum_qty += abs(l.qty)
                sum_price += abs(l.qty) * l.price
            self.trade.entry_price = sum_price / sum_qty

            # calculate average exit_price
            sum_price = 0
            sum_qty = 0
            for l in self.trade.orders:
                if not l.is_executed:
                    continue

                if jh.side_to_type(l.side) == self.trade.type:
                    continue

                sum_qty += abs(l.qty)
                sum_price += abs(l.qty) * l.price
            self.trade.exit_price = sum_price / sum_qty

            self.trade.closed_at = jh.now()
            self.trade.qty = pydash.sum_by(
                filter(lambda o: o.side == jh.type_to_side(self.trade.type), self.trade.orders),
                lambda o: abs(o.qty)
            )

            store.completed_trades.add_trade(self.trade)
            self.trade = None
            self.trades_count += 1

    @property
    def is_long(self):
        return self.position.type == 'long'

    @property
    def is_short(self):
        return self.position.type == 'short'

    @property
    def is_open(self):
        return self.position.is_open

    @property
    def is_close(self):
        return self.position.is_close

    @property
    def average_stop_loss(self) -> float:
        if self._stop_loss is None:
            raise exceptions.InvalidStrategy('You cannot access self.average_stop_loss before setting self.stop_loss')

        arr = self._stop_loss
        return (np.abs(arr[:, 0] * arr[:, 1])).sum() / np.abs(arr[:, 0]).sum()

    @property
    def average_take_profit(self) -> float:
        if self._take_profit is None:
            raise exceptions.InvalidStrategy('You cannot access self.average_take_profit before setting self.take_profit')

        arr = self._take_profit
        return (np.abs(arr[:, 0] * arr[:, 1])).sum() / np.abs(arr[:, 0]).sum()

    @property
    def average_entry_price(self):
        if self.is_long:
            arr = self._buy
        elif self.is_short:
            arr = self._sell
        elif self.should_long():
            arr = self._buy
        elif self.should_short():
            arr = self._sell
        else:
            return None

        return (np.abs(arr[:, 0] * arr[:, 1])).sum() / np.abs(arr[:, 0]).sum()

    def liquidate(self):
        """
        closes open position with a MARKET order
        """
        if self.position.is_close:
            return

        if self.position.pnl > 0:
            self.take_profit = self.position.qty, self.price
        else:
            self.stop_loss = self.position.qty, self.price

    @property
    def shared_vars(self):
        return store.vars
