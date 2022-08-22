import hashlib
import hmac
from pathlib import Path
import csv
import json
from datetime import datetime, timedelta
from time import time
from typing import Any, Dict, List, Callable
from threading import Lock
from copy import copy
from vnpy.trader.database import database_manager
from peewee import chunked
from requests import ConnectionError
import pandas as pd
from uuid import uuid4
import pickle
import redis
import zlib
from vnpy.api.websocket import WebsocketClient
from vnpy.api.rest import Request, RestClient
from vnpy.trader.constant import (
    Exchange,
    Interval,
    OrderType,
    Product,
    Status,
    Direction,
    Offset
)
from vnpy.trader.object import (
    AccountData,
    BarData,
    TickData,
    OrderData,
    TradeData,
    ContractData,
    PositionData,
    HistoryRequest,
    SubscribeRequest,
    CancelRequest,
    OrderRequest
)
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.gateway import BaseGateway, LocalOrderManager
from vnpy.trader.utility import (save_connection_status,delete_dr_data,get_folder_path,load_json, save_json,remain_digit,get_symbol_mark,get_local_datetime,extract_vt_symbol,TZ_INFO,publish_redis_data,GetFilePath)
from vnpy.trader.setting import bybit_account #导入账户字典bybit_account

STATUS_BYBIT2VT = {
    "Created": Status.NOTTRADED,
    "New": Status.NOTTRADED,
    "PartiallyFilled": Status.PARTTRADED,
    "Filled": Status.ALLTRADED,
    "Cancelled": Status.CANCELLED,
    "Rejected": Status.REJECTED,
}

DIRECTION_VT2BYBIT = {Direction.LONG: "Buy", Direction.SHORT: "Sell"}
DIRECTION_BYBIT2VT = {v: k for k, v in DIRECTION_VT2BYBIT.items()}

OPPOSITE_DIRECTION = {
    Direction.LONG: Direction.SHORT,
    Direction.SHORT: Direction.LONG,
}

ORDER_TYPE_VT2BYBIT = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
}
ORDER_TYPE_BYBIT2VT = {v: k for k, v in ORDER_TYPE_VT2BYBIT.items()}

INTERVAL_VT2BYBIT = {
    Interval.MINUTE: "1",
    Interval.HOUR: "60",
    Interval.DAILY: "D",
    Interval.WEEKLY: "W",
}

TIMEDELTA_MAP = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
    Interval.WEEKLY: timedelta(days=7),
}

REST_HOST = "https://api.bybit.com"         # 主host  https://api.bybit.com备用host https://api.bybitglobal.com
PUBLIC_WS_HOST = "wss://stream.bybit.com/perpetual/ws/v1/realtime_public"   #主网公共topic地址
PRIVATE_WS_HOST = "wss://stream.bybit.com/trade/option/usdc/private/v1"  #主网私有topic地址

TESTNET_REST_HOST = "https://api-testnet.bybit.com"
TESTNET_PUBLIC_WS_HOST = "wss://stream-testnet.bybit.com/perpetual/ws/v1/realtime_public"
TESTNET_PRIVATE_WS_HOST = "wss://stream-testnet.bybit.com/trade/option/usdc/private/v1"
#-------------------------------------------------------------------------------------------------   
class BybitUsdcGateway(BaseGateway):
    """
    BYBIT USDC合约接口
    """
    #default_setting由vnpy.trader.ui.widget调用
    default_setting = {
        "ID": "",
        "Secret": "",
        "服务器": ["REAL", "TESTNET"],
        "代理地址": "",
        "代理端口": "",
    }

    exchanges = [Exchange.BYBIT]      #由main_engine add_gateway调用
    #所有合约列表
    recording_list = GetFilePath.recording_list
    #-------------------------------------------------------------------------------------------------   
    def __init__(self, event_engine):
        """
        """
        super().__init__(event_engine, "BYBITUSDC")
        self.orders: Dict[str, OrderData] = {}
        self.connect_time = datetime.now(TZ_INFO).strftime("%y%m%d%H%M%S")
        self.order_manager = LocalOrderManager(self, self.connect_time)

        self.rest_api = BybitRestApi(self)
        self.ws_data_api = BybitWebsocketDataApi(self)
        self.ws_trade_api = BybitWebsocketTradeApi(self)
        self.recording_list = [vt_symbol for vt_symbol in self.recording_list if extract_vt_symbol(vt_symbol)[2] == self.gateway_name]
        #历史数据合约列表
        self.history_contracts = copy(self.recording_list)
        #活动委托单合约列表
        self.active_contracts = copy(self.recording_list)
        #仓位合约列表
        self.position_contracts = copy(self.recording_list)
    #-------------------------------------------------------------------------------------------------   
    def connect(self,log_account:dict = {}):
        """
        """
        if not log_account:
            log_account = bybit_account
        key = log_account["APIKey"]
        secret = log_account["PrivateKey"]
        server = log_account["服务器"]
        proxy_host = log_account["代理地址"]
        proxy_port = log_account["代理端口"]
        proxy_type = log_account["proxy_type"]
        publish_status = log_account["行情分发"]
        self.account_file_name = log_account["account_file_name"]
        self.rest_api.connect(key, secret, server, proxy_host, proxy_port,proxy_type)
        self.ws_data_api.connect(server, proxy_host, proxy_port,proxy_type,publish_status)
        self.ws_trade_api.connect(key, secret, server, proxy_host, proxy_port,proxy_type)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_TIMER, self.query_history)
    #-------------------------------------------------------------------------------------------------   
    def subscribe(self, req: SubscribeRequest):
        """
        """
        self.ws_data_api.subscribe(req)
    #-------------------------------------------------------------------------------------------------   
    def send_order(self, req: OrderRequest):
        """
        """
        return self.rest_api.send_order(req)
    #-------------------------------------------------------------------------------------------------   
    def cancel_order(self, req: CancelRequest):
        """
        """
        self.rest_api.cancel_order(req)
    #-------------------------------------------------------------------------------------------------   
    def query_account(self):
        """
        """
        self.rest_api.query_account()
    #------------------------------------------------------------------------------------------------- 
    def query_order(self,symbol:str):
        """
        查询未成交委托单
        """
        self.rest_api.query_active_order(symbol)
    #------------------------------------------------------------------------------------------------- 
    def query_position(self,symbol:str):
        """
        查询持仓
        """
        self.rest_api.query_position(symbol)
    #-------------------------------------------------------------------------------------------------   
    def process_timer_event(self,event):
        """
        处理定时任务
        """
        self.query_account()
        if self.position_contracts:
            symbol,exchange,gateway_name = extract_vt_symbol(self.position_contracts.pop(0))
            self.query_order(symbol)
            self.query_position(symbol)
        else:
            self.position_contracts = copy(self.recording_list)
    #-------------------------------------------------------------------------------------------------   
    def query_history(self,event):
        """
        查询合约历史数据
        """
        if self.history_contracts:
            symbol,exchange,gateway_name = extract_vt_symbol(self.history_contracts.pop(0))
            req = HistoryRequest(
                symbol = symbol,
                exchange = Exchange(exchange),
                interval = Interval.MINUTE,
                start = datetime.now(TZ_INFO) - timedelta(days = 1),
                gateway_name = self.gateway_name
            )
            self.rest_api.query_history(req)
            self.rest_api.set_leverage(symbol)
    #---------------------------------------------------------------------------------------
    def on_order(self, order: OrderData) -> None:
        """
        收到委托单推送，BaseGateway推送数据
        """
        self.orders[order.vt_orderid] = copy(order)
        super().on_order(order)
    #---------------------------------------------------------------------------------------
    def get_order(self, vt_orderid: str) -> OrderData:
        """
        用vt_orderid获取委托单数据
        """
        return self.orders.get(vt_orderid, None)
    #-------------------------------------------------------------------------------------------------   
    def close(self):
        """
        """
        self.rest_api.stop()
        self.ws_data_api.stop()
        self.ws_trade_api.stop()
#-------------------------------------------------------------------------------------------------   
class BybitRestApi(RestClient):
    """
    ByBit REST API
    """
    #-------------------------------------------------------------------------------------------------   
    def __init__(self, gateway: BybitUsdcGateway):
        """
        """
        super().__init__()
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.order_manager = gateway.order_manager
        self.key = ""
        self.secret = b""
        self.account_date = None    #账户日期
        self.accounts_info:Dict[str,dict] = {}
        self.all_contracts:List[str] = []          #所有vt_symbol合约列表
    #-------------------------------------------------------------------------------------------------   
    def get_server_time(self):
        """
        获取服务器时间
        """
        self.add_request(
            "GET",
            "/v2/public/time",
            callback=self.on_server_time,
            )
    #-------------------------------------------------------------------------------------------------   
    def on_server_time(self,data: dict, request: Request):
        """
        收到服务器时间回报
        """
        server_time = get_local_datetime(float(data["time_now"]))
        local_time = datetime.now(TZ_INFO)
        self.gateway.write_log(f"服务器时间：{server_time}，本地时间：{local_time}")
    #-------------------------------------------------------------------------------------------------   
    def sign(self, request: Request):
        """
        Generate ByBit signature.
        """

        if request.method == "GET":
            api_params = request.params
            if not api_params:
                api_params = request.params = {}
            else:
                api_params = json.dumps(api_params)
        else:
            api_params = request.data
            if not api_params:
                api_params = request.data = {}
            else:
                api_params = json.dumps(api_params)
                request.data = api_params

        recv_window = str(5000)
        nonce = generate_timestamp(-5)
        if not api_params:
            api_params = request.data = json.dumps({})
        param_str= str(nonce) + self.key + recv_window + api_params
        signature = hmac.new(self.secret, param_str.encode("utf-8"),hashlib.sha256).hexdigest()
        if request.headers is None:
            request.headers = {"Content-Type": "application/json"}
        request.headers["X-BAPI-API-KEY"] = self.key
        request.headers["X-BAPI-SIGN"] = signature
        request.headers["X-BAPI-TIMESTAMP"] = str(nonce)
        request.headers["X-BAPI-RECV-WINDOW"] = recv_window
        request.headers["X-BAPI-SIGN-TYPE"] = "2"
        return request
    #-------------------------------------------------------------------------------------------------   
    def connect(
        self,
        key: str,
        secret: str,
        server: str,
        proxy_host: str,
        proxy_port: int,
        proxy_type:str,
    ):
        """
        Initialize connection to REST server.
        """
        self.key = key
        self.secret = secret.encode()
        if server == "REAL":
            self.init(REST_HOST, proxy_host, proxy_port,proxy_type,gateway_name = self.gateway_name)
        else:
            self.init(TESTNET_REST_HOST, proxy_host, proxy_port,proxy_type,gateway_name = self.gateway_name)

        self.start(3)
        self.gateway.write_log(f"交易接口:{self.gateway_name},REST API启动成功")
        self.get_server_time()
        self.query_contract()
    #-------------------------------------------------------------------------------------------------  
    def set_leverage(self,symbol:str):
        """
        设置合约杠杆
        """
        path = "/perpetual/usdc/openapi/private/v1/position/leverage/save"
        data = {"symbol":symbol,"leverage":20}
        self.add_request("POST", path,self.on_leverage,data = data,extra=data)
    #-------------------------------------------------------------------------------------------------   
    def on_leverage(self, data: dict, request: Request):
        """
        收到设置杠杆回调
        """
        pass
    #-------------------------------------------------------------------------------------------------   
    def send_order(self, req: OrderRequest):
        """
        """
        orderId = req.symbol + "-" + self.order_manager.new_local_orderid()
        data = {
            "symbol": req.symbol,
            "orderPrice":str(req.price),
            "orderQty": str(req.volume),
            "orderFilter": "Order",
            "side": DIRECTION_VT2BYBIT[req.direction],
            "orderType":ORDER_TYPE_VT2BYBIT[req.type],
            "orderLinkId": orderId,
            "timeInForce": "GoodTillCancel",
        }
        #平仓信号仅减仓
        if req.offset == Offset.CLOSE:
            data["reduceOnly"] = True
            data["closeOnTrigger"] = True
        order = req.create_order_data(orderId, self.gateway_name)
        order.datetime = datetime.now(TZ_INFO)

        self.add_request(
            "POST",
            "/perpetual/usdc/openapi/private/v1/place-order",
            callback=self.on_send_order,
            data=data,
            extra=order,
            on_failed=self.on_send_order_failed,
            on_error=self.on_send_order_error,
        )

        self.order_manager.on_order(order)
        return order.vt_orderid
    #-------------------------------------------------------------------------------------------------   
    def on_send_order_failed(self, status_code, request: Request):
        """
        Callback when sending order failed on server.
        """
        order = request.extra
        order.status = Status.REJECTED
        self.order_manager.on_order(order)
        data = request.response.json()
        error_msg = data["retMsg"]
        error_code = data["retCode"]
        msg = f"发送委托失败，错误代码:{error_code},  错误信息：{error_msg}，委托单数据：{order}"
        self.gateway.write_log(msg)
    #-------------------------------------------------------------------------------------------------   
    def on_send_order_error(
        self, exception_type: type, exception_value: Exception, tracebacks, request: Request
    ):
        """
        Callback when sending order caused exception.
        """
        order:OrderData = request.extra
        order.status = Status.REJECTED
        self.order_manager.on_order(order)

        # Record exception if not ConnectionError
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tracebacks, request)
    #-------------------------------------------------------------------------------------------------   
    def on_send_order(self, data: dict, request: Request):
        """
        """
        if self.check_error("发送委托", data):
            order:OrderData = request.extra
            order.status = Status.REJECTED
            self.order_manager.on_order(order)
            self.gateway.write_log(f"错误委托单：{order}")
            return
        result = data["result"]
        self.order_manager.update_orderid_map(
            result["orderLinkId"],
            result["orderId"]
        )
    #-------------------------------------------------------------------------------------------------   
    def cancel_order(self, req: CancelRequest):
        """
        """
        order: OrderData = self.gateway.get_order(req.vt_orderid)
        sys_orderid = self.order_manager.get_sys_orderid(req.orderid)
        
        data = {
            "orderFilter":"Order",
            "orderId": sys_orderid,
            "symbol": req.symbol,
        }

        self.add_request(
            "POST",
            path="/perpetual/usdc/openapi/private/v1/cancel-order",
            data=data,
            callback=self.on_cancel_order,
            on_failed=self.on_cancel_failed,
            extra=order
        )
    #-------------------------------------------------------------------------------------------------   
    def on_cancel_order(self, data: dict, request: Request):
        """
        """
        if self.check_error("取消委托", data):
            error_code = data["retCode"]
            # 重复撤销委托单被拒推送
            if error_code == 20001:
                order: OrderData= request.extra
                order.status = Status.REJECTED
                self.order_manager.on_order(order)
            return
    #---------------------------------------------------------------------------------------
    def on_cancel_failed(self, status_code, request: Request) -> None:
        """
        收到取消委托单失败回报
        """
        if request.extra:
            order = request.extra
            order.status = Status.REJECTED
            self.gateway.on_order(order)

        msg = f"撤单失败，状态码：{status_code}，错误信息：{request.response.text}"
        self.gateway.write_log(msg)
    #-------------------------------------------------------------------------------------------------   
    def query_contract(self):
        """
        """
        params = {"direction":"next"}
        self.add_request( "GET", "/perpetual/usdc/openapi/public/v1/symbols", self.on_query_contract,params)
    #-------------------------------------------------------------------------------------------------
    def get_id(self):
        """
        获取唯一id
        """
        id_ = str(uuid4()).replace('-', '')
        return id_
    #-------------------------------------------------------------------------------------------------   
    def check_error(self, name: str, data: dict):
        """
        """
        if data["retCode"]:
            error_code = data["retCode"]
            error_msg = data["retMsg"]
            msg = f"{name}失败，错误代码：{error_code}，信息：{error_msg}"
            self.gateway.write_log(msg)
            return True

        return False
    #-------------------------------------------------------------------------------------------------   
    def on_query_contract(self, data: dict, request: Request):
        """
        查询合约
        """
        if self.check_error("查询合约", data):
            return

        for contract_data in data["result"]:
            #只处理PERP合约
            if "PERP" not in contract_data["symbol"]:
                continue
            contract = ContractData(
                symbol=contract_data["symbol"],
                exchange=Exchange.BYBIT,
                name=contract_data["symbol"],
                product=Product.FUTURES,
                size = 20,             #合约杠杆
                price_tick=float(contract_data["minPrice"]),
                max_volume = float(contract_data["maxTradingQty"]),
                min_volume=float(contract_data["minTradingQty"]),
                open_commission_ratio = float(contract_data["takerFeeRate"]),     #takerFeeRate市价平仓手续费率，makerFeeRate挂单平仓手续费率
                close_commission_ratio = float(contract_data["takerFeeRate"]),
                gateway_name=self.gateway_name
            )
            self.gateway.on_contract(contract)
            if contract.vt_symbol not in self.all_contracts:
                self.all_contracts.append(contract.vt_symbol)    
        self.gateway.on_all_contracts(self.all_contracts)
        self.gateway.write_log(f"{self.gateway_name}，合约信息查询成功")
    #-------------------------------------------------------------------------------------------------  
    def query_account(self):
        """
        发送查询资金请求
        """
        self.add_request(method = "POST", path = "/option/usdc/openapi/private/v1/query-wallet-balance", callback = self.on_query_account)
    #------------------------------------------------------------------------------------------------- 
    def on_query_account(self,data: dict, request: Request):
        """
        收到资金回报
        """
        if data["retCode"] == 10016:
            return
        if not data["result"]:
            return
        data = data["result"]
        account = AccountData(
            accountid= f"USDC_{self.gateway_name}",
            balance= float(data["walletBalance"]),
            available = float(data["availableBalance"]),
            margin = float(data["accountMM"]),
            position_profit = float(data["totalSessionUPL"]),
            close_profit = float(data["totalRPL"]),
            datetime = datetime.now(TZ_INFO),
            gateway_name= self.gateway_name
        )
        if account.balance:
            self.gateway.on_account(account)
            #保存账户资金信息
            self.accounts_info[account.accountid] = account.__dict__
        if  not self.accounts_info:
            return
        accounts_info = list(self.accounts_info.values())
        account_date = accounts_info[-1]["datetime"].date()
        account_path = GetFilePath().ctp_account_path.replace("ctp_account_1",self.gateway.account_file_name)
        for account_data in accounts_info:
            if not Path(account_path).exists(): # 如果文件不存在，需要写header
                with open(account_path, 'w',newline="") as f1:          #newline=""不自动换行
                    w1 = csv.DictWriter(f1, account_data.keys())
                    w1.writeheader()
                    w1.writerow(account_data)
            else: # 文件存在，不需要写header
                if self.account_date and self.account_date != account_date:        #一天写入一次账户信息         
                    with open(account_path,'a',newline="") as f1:                               #a二进制追加形式写入
                        w1 = csv.DictWriter(f1, account_data.keys())
                        w1.writerow(account_data)
        self.account_date = account_date
    #-------------------------------------------------------------------------------------------------   
    def query_position(self,symbol:str):
        """
        发送查询持仓请求
        """
        data={
            "category":"PERPETUAL",
            "direction":"next",
            "symbol":symbol
        }
        self.add_request(method = "POST", path = "/option/usdc/openapi/private/v1/query-position", callback = self.on_query_position,data = data)
    #-------------------------------------------------------------------------------------------------   
    def on_query_position(self, data: dict, request: Request):
        """
        收到持仓回报
        """
        if self.check_error("查询持仓", data):
            if data["retCode"] == "10001":
                delete_dr_data(request.params["symbol"],self.gateway_name)
            return
        raw_data = data["result"]["dataList"]
        for pos_data in raw_data:
            #冻结仓位
            if pos_data["side"] == "Buy":
                long_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.LONG,
                    volume=float(pos_data["size"]),
                    price=float(pos_data["entryPrice"]),  
                    pnl = float(pos_data["unrealisedPnl"]),              #持仓盈亏
                    gateway_name=self.gateway_name
                )   
                self.gateway.on_position(long_position)
            elif pos_data["side"] == "Sell":     
                short_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.SHORT,
                    volume=float(pos_data["size"]),
                    price=float(pos_data["entryPrice"]),  
                    pnl = float(pos_data["unrealisedPnl"]),         
                    gateway_name=self.gateway_name                
                )
                self.gateway.on_position(short_position)
            else:
                long_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.LONG,
                    volume = 0,
                    price = 0,
                    pnl = 0,
                    frozen = 0,
                    gateway_name=self.gateway_name,
                    )
                short_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.SHORT,
                    volume = 0,
                    price = 0,
                    pnl = 0,
                    frozen = 0,
                    gateway_name=self.gateway_name,
                    )
                self.gateway.on_position(long_position)
                self.gateway.on_position(short_position)
    #-------------------------------------------------------------------------------------------------   
    def query_active_order(self,symbol:str):
        """
        发送查询活动委托单请求
        """
        data = {
            "category":"PERPETUAL",
            "limit": 50,
            "symbol":symbol,
            "direction":"next"
        }

        self.add_request( "POST", "/option/usdc/openapi/private/v1/query-active-orders", callback=self.on_query_order, data=data )
    #-------------------------------------------------------------------------------------------------   
    def on_query_order(self, data: dict, request: Request):
        """
        收到活动委托单回报
        """
        if self.check_error("查询未成交委托", data):
            if data["retCode"] == "10001":
                delete_dr_data(request.params["symbol"],self.gateway_name)
            return
        result = data["result"]["dataList"]
        if not result:
            return
        for order_data in result:
            sys_orderid = order_data["orderId"]
            order = self.order_manager.get_order_with_sys_orderid(sys_orderid)

            if order:
                order.traded = float(order_data["cumExecQty"]) if order_data["cumExecQty"] else 0
                order.status = STATUS_BYBIT2VT[order_data["orderStatus"]]
            else:
                # Use sys_orderid as local_orderid when
                # order placed from other source
                local_orderid = order_data["orderLinkId"]
                order_datetime = get_local_datetime(order_data["createdAt"])
                if not local_orderid:
                    local_orderid = sys_orderid

                self.order_manager.update_orderid_map(
                    local_orderid,
                    sys_orderid
                )

                order = OrderData(
                    symbol=order_data["symbol"],
                    exchange=Exchange.BYBIT,
                    orderid=local_orderid,
                    type=ORDER_TYPE_BYBIT2VT[order_data["orderType"]],
                    direction=DIRECTION_BYBIT2VT[order_data["side"]],
                    price=float(order_data["price"]),
                    volume=float(order_data["qty"]),
                    traded=float(order_data["cumExecQty"]) if order_data["cumExecQty"] else 0,
                    status=STATUS_BYBIT2VT[order_data["orderStatus"]],
                    datetime= order_datetime,
                    gateway_name=self.gateway_name
                )
                if "reduceOnly" in order_data and order_data["reduceOnly"]:
                    order.offset = Offset.CLOSE
            self.order_manager.on_order(order)
    #-------------------------------------------------------------------------------------------------   
    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """
        查询历史数据
        """
        history = []
        count = 200
        start_time = int(req.start.timestamp())
        time_consuming_start = time()
        while True:
            # Create query params
            params = {
                "symbol": req.symbol,
                "period": INTERVAL_VT2BYBIT[req.interval],
                "startTime": start_time,
                "limit": count
            }

            # Get response from server
            resp = self.request(
                "GET",
                "/perpetual/usdc/openapi/public/v1/kline/list",
                params=params
            )

            # Break if request failed with other status code
            if not resp:
                msg = f"合约：{req.vt_symbol}，获取历史数据失败"
                self.gateway.write_log(msg)
                continue
            elif resp.status_code // 100 != 2:
                msg = f"合约：{req.vt_symbol}，获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                continue
            else:
                data = resp.json()
                if not data["result"]:
                    msg = f"合约：{req.vt_symbol}，获取历史数据为空，开始时间：{start_time}，数量：{count}"
                    delete_dr_data(req.symbol,self.gateway_name)
                    continue

                buf = []
                for data in data["result"]:
                    dt = get_local_datetime(int(data["openTime"]))
                    bar = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=float(data["volume"]),
                        open_price=float(data["open"]),
                        high_price=float(data["high"]),
                        low_price=float(data["low"]),
                        close_price=float(data["close"]),
                        gateway_name=self.gateway_name
                    )
                    buf.append(bar)

                history.extend(buf)
                # Break if last data collected
                if len(buf) < count:
                    break
                # Update start time
                start_time = int((bar.datetime + TIMEDELTA_MAP[req.interval]).timestamp())
        for bar_data in chunked(history, 10000):               #分批保存数据
            try:
                database_manager.save_bar_data(bar_data,True)      #保存数据到数据库  
            except Exception as err:
                self.gateway.write_log(f"{err}")
                return    
        time_consuming_end =time()        
        query_time = round(time_consuming_end - time_consuming_start,3)
        msg = f"载入{req.vt_symbol}:bar数据，开始时间：{history[0].datetime} ，结束时间： {history[-1].datetime}，数据量：{len(history)}，耗时:{query_time}秒"
        self.gateway.write_log(msg)
#-------------------------------------------------------------------------------------------------   
class BybitWebsocketDataApi(WebsocketClient):
    """
    """

    def __init__(self, gateway: BybitUsdcGateway):
        """
        """
        super().__init__()

        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

        self.server: str = ""  # REAL or TESTNET

        self.callbacks: Dict[str, Callable] = {}
        self.ticks: Dict[str, TickData] = {}
        self.subscribed: Dict[str, SubscribeRequest] = {}

        self.symbol_bids: Dict[str, dict] = {}
        self.symbol_asks: Dict[str, dict] = {}

        self.publish_status = False
    #-------------------------------------------------------------------------------------------------   
    def connect(
        self, server: str, proxy_host: str, proxy_port: int,proxy_type:str,publish_status:bool
    ):
        """
        """
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.server = server
        self.publish_status = publish_status
        if self.server == "REAL":
            url =  PUBLIC_WS_HOST
        else:
            url = TESTNET_PUBLIC_WS_HOST

        self.init(url, self.proxy_host, self.proxy_port,proxy_type,gateway_name = self.gateway_name)
        self.start()
    #-------------------------------------------------------------------------------------------------   
    def subscribe(self, req: SubscribeRequest):
        """
        Subscribe to tick data update.
        """
        self.subscribed[req.vt_symbol] = req

        tick = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            datetime=datetime.now(TZ_INFO),
            name=req.symbol,
            gateway_name=self.gateway_name
        )
        self.ticks[req.symbol] = tick

        self.subscribe_topic(f"instrument_info.100ms.{req.symbol}", self.on_tick)
        self.subscribe_topic(f"orderBookL2_25.{req.symbol}", self.on_depth)
    #-------------------------------------------------------------------------------------------------   
    def subscribe_topic(self, topic: str, callback: Callable[[str, dict], Any]):
        """
        订阅私有主题
        """
        self.callbacks[topic] = callback

        req = {
            "op": "subscribe",
            "args": [topic],
        }
        self.send_packet(req)
    #-------------------------------------------------------------------------------------------------   
    def on_connected(self):
        """
        """
        self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API行情连接成功")
        for req in list(self.subscribed.values()):
            self.subscribe(req)
    #-------------------------------------------------------------------------------------------------   
    def on_disconnected(self):
        """
        """
        self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API行情连接断开")
    #-------------------------------------------------------------------------------------------------   
    def on_packet(self, packet: dict):
        """
        """
        if "ret_msg" in packet:
            # 登录成功
            if packet["ret_msg"] == "0":
                self.on_login(packet)
        else:
            # 过滤响应请求
            if packet.get("type",None) != "COMMAND_RESP":
                channel = packet["topic"]            
                callback = self.callbacks[channel]
                callback(packet)
    #-------------------------------------------------------------------------------------------------   
    def on_tick(self, packet: dict):
        """     
        """
        topic = packet["topic"]
        type_ = packet["type"]
        data = packet["data"]
        timestamp = packet["timestampE6"]
        symbol = topic.replace("instrument_info.100ms.", "")
        tick = self.ticks[symbol]
        if type_ == "snapshot":
            if not float(data["lastPrice"]):
                return
            tick.last_price = float(data["lastPrice"])
            tick.volume = float(data["volume24hE8"]) / 1e8
            tick.high_price = float(data["highPrice24h"])
            tick.low_price = float(data["lowPrice24h"])
            tick.pre_close = float(data["prevPrice24h"])
            tick.open_interest = float(data["openInterestE8"]) /1e8
        else:
            update = data["update"][0]
            if "lastPrice" in update:
                if not float(update["lastPrice"]):
                    return
                tick.last_price = float(update["lastPrice"])
            if "volume24hE8" in update:
                tick.volume = float(update["volume24hE8"]) / 1e8
            if "highPrice24h" in update:
                tick.high_price = float(update["highPrice24h"])
            if "lowPrice24h" in update:
                tick.low_price = float(update["lowPrice24h"])
            if "prevPrice24h" in update:     
                tick.pre_close = float(update["prevPrice24h"])
            if "openInterestE8" in update:
                tick.open_interest = float(update["openInterestE8"])  / 1e8
        tick.datetime = get_local_datetime(float(timestamp) / 1e6)
        new_tick = copy(tick)
        if new_tick.last_price:
            self.gateway.on_tick(new_tick)
        
        if self.publish_status:
            #redis发布tick数据
            publish_redis_data(new_tick)
    #-------------------------------------------------------------------------------------------------   
    def on_depth(self, packet: dict):
        """
        """
        topic = packet["topic"]
        data = packet["data"]
        type_ = packet["type"]
        timestamp = float(packet["timestampE6"])

        # 更新深度数据到bids，asks
        symbol = topic.replace("orderBookL2_25.", "")
        tick = self.ticks[symbol]
        bids = self.symbol_bids.setdefault(symbol, {})
        asks = self.symbol_asks.setdefault(symbol, {})
        # 清除过期合约
        if not data:
            delete_dr_data(tick.symbol,self.gateway_name)
            return
        #全量推送
        if type_ == "snapshot":
            for depth_data in data["orderBook"]:
                price = float(depth_data["price"])

                if depth_data["side"] == "Buy":
                    bids[price] = depth_data
                else:
                    asks[price] = depth_data
        else:
            #增量推送
            for depth_data in data["delete"]:
                price = float(depth_data["price"])
                if depth_data["side"] == "Buy":
                    if price in bids:
                        bids.pop(price)
                else:
                    if price in asks:
                        asks.pop(price)
            for depth_data in (data["update"] + data["insert"]):
                price = float(depth_data["price"])
                if depth_data["side"] == "Buy":
                    bids[price] = depth_data
                else:
                    asks[price] = depth_data

        # Calculate 1-5 bid/ask depth
        bid_keys = list(bids.keys())
        bid_keys.sort(reverse=True)

        ask_keys = list(asks.keys())
        ask_keys.sort()

        for index in range(5):
            attr = index + 1
            bid_price = bid_keys[index]
            bid_data = bids[bid_price]
            ask_price = ask_keys[index]
            ask_data = asks[ask_price]
            setattr(tick, f"bid_price_{attr}", bid_price)
            setattr(tick, f"bid_volume_{attr}", bid_data.get("size",0))
            setattr(tick, f"ask_price_{attr}", ask_price)
            setattr(tick, f"ask_volume_{attr}", ask_data.get("size",0))
        tick.datetime = get_local_datetime(timestamp / 1e6)
        new_tick = copy(tick)
        if new_tick.last_price:
            self.gateway.on_tick(new_tick)
#-------------------------------------------------------------------------------------------------   
class BybitWebsocketTradeApi(WebsocketClient):
    def __init__(self, gateway: BybitUsdcGateway):
        """
        """
        super().__init__()
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.order_manager = gateway.order_manager

        self.key = ""
        self.secret = b""
        self.callbacks: Dict[str, Callable] = {}
    #-------------------------------------------------------------------------------------------------   
    def connect(
        self, key: str, secret: str, server: str, proxy_host: str, proxy_port: int,proxy_type:str,
    ):
        """
        """
        self.key = key
        self.secret = secret.encode()
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.server = server

        if self.server == "REAL":
            url =  PRIVATE_WS_HOST
        else:
            url = TESTNET_PRIVATE_WS_HOST

        self.init(url, self.proxy_host, self.proxy_port,proxy_type,gateway_name = self.gateway_name)
        self.start()
    #-------------------------------------------------------------------------------------------------   
    def login(self):
        """
        """
        expires = generate_timestamp(30)
        msg = f"GET/realtime{int(expires)}"
        signature = sign(self.secret, msg.encode())

        req = {
            "op": "auth",
            "args": [self.key, expires, signature]
        }
        self.send_packet(req)
    #-------------------------------------------------------------------------------------------------   
    def on_login(self, packet: dict):
        """
        收到登录回报
        """
        success = packet.get("success", False)
        if success:
            self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API登录成功")

            self.subscribe_topic("user.openapi.perp.order", self.on_order)
            self.subscribe_topic("user.openapi.perp.trade", self.on_trade)
            self.subscribe_topic("user.openapi.perp.position", self.on_position)
        else:
            self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API登录失败")
    #-------------------------------------------------------------------------------------------------   
    def subscribe_topic(self, topic: str, callback: Callable[[str, dict], Any]):
        """
        Subscribe to all private topics.
        """
        self.callbacks[topic] = callback

        req = {
            "op": "subscribe",
            "args": [topic],
        }
        self.send_packet(req)
    #-------------------------------------------------------------------------------------------------   
    def on_packet(self, packet: dict):
        """
        """
        if "ret_msg" in packet:
            # 登录成功
            if packet["ret_msg"] == "0":
                self.on_login(packet)
        else:
            # 过滤响应请求
            if packet.get("type",None) != "COMMAND_RESP":
                channel = packet["topic"]            
                callback = self.callbacks[channel]
                callback(packet)
    #-------------------------------------------------------------------------------------------------   
    def on_connected(self):
        """
        """
        self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API交易连接成功")
        self.login()
    #-------------------------------------------------------------------------------------------------   
    def on_disconnected(self):
        """
        """
        self.gateway.write_log(f"交易接口:{self.gateway_name},Websocket API交易连接断开")
    #-------------------------------------------------------------------------------------------------   
    def on_trade(self, packet):
        """
        """
        for trade_data in packet["data"]["result"]:
            orderId = trade_data["orderLinkId"]
            if not orderId:
                orderId = trade_data["orderId"]
            trade_datetime = get_local_datetime(trade_data["tradeTime"])        
            trade = TradeData(
                symbol=trade_data["symbol"],
                exchange=Exchange.BYBIT,
                orderid=orderId,
                tradeid=trade_data["tradeId"],
                direction=DIRECTION_BYBIT2VT[trade_data["side"]],
                price=float(trade_data["execPrice"]),
                volume=float(trade_data["execQty"]),
                datetime =trade_datetime,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)
    #-------------------------------------------------------------------------------------------------   
    def on_order(self, packet):
        """
        """
        for order_data in packet["data"]["result"]:
            sys_orderid = order_data["orderId"]
            order = self.order_manager.get_order_with_sys_orderid(sys_orderid)

            if order:
                order.traded = float(order_data["cumExecQty"]) if order_data["cumExecQty"] else 0
                order.status = STATUS_BYBIT2VT[order_data["orderStatus"]]
            else:
                # Use sys_orderid as local_orderid when
                # order placed from other source
                local_orderid = order_data["orderLinkId"]
                order_datetime = get_local_datetime(order_data["createdAt"])
                if not local_orderid:
                    local_orderid = sys_orderid

                self.order_manager.update_orderid_map(
                    local_orderid,
                    sys_orderid
                )

                order = OrderData(
                    symbol=order_data["symbol"],
                    exchange=Exchange.BYBIT,
                    orderid=local_orderid,
                    type=ORDER_TYPE_BYBIT2VT[order_data["orderType"]],
                    direction=DIRECTION_BYBIT2VT[order_data["side"]],
                    price=float(order_data["price"]),
                    volume=float(order_data["qty"]),
                    traded=float(order_data["cumExecQty"]) if order_data["cumExecQty"] else 0,
                    status=STATUS_BYBIT2VT[order_data["orderStatus"]],
                    datetime= order_datetime,
                    gateway_name=self.gateway_name
                )
                if order_data["reduceOnly"]:
                    order.offset = Offset.CLOSE
            self.order_manager.on_order(order)
    #-------------------------------------------------------------------------------------------------   
    def on_position(self, packet):
        """
        收到持仓回报
        """
        for pos_data in packet["data"]["result"]:

            if pos_data["side"] == "Buy":
                long_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.LONG,
                    volume=float(pos_data["size"]),
                    price=float(pos_data["entryPrice"]),
                    pnl = float(pos_data["unrealisedPnl"]),
                    gateway_name=self.gateway_name
                )   
                self.gateway.on_position(long_position)
            elif pos_data["side"] == "Sell":    
                short_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.SHORT,
                    volume=float(pos_data["size"]),
                    price=float(pos_data["entryPrice"]),
                    pnl = float(pos_data["unrealisedPnl"]),
                    gateway_name=self.gateway_name                
                )
                self.gateway.on_position(short_position)
            else:
                long_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.LONG,
                    volume = 0,
                    price = 0,
                    pnl = 0,
                    frozen = 0,
                    gateway_name=self.gateway_name,
                    )
                short_position = PositionData(
                    symbol=pos_data["symbol"],
                    exchange=Exchange.BYBIT,
                    direction=Direction.SHORT,
                    volume = 0,
                    price = 0,
                    pnl = 0,
                    frozen = 0,
                    gateway_name=self.gateway_name,
                    )
                self.gateway.on_position(long_position)
                self.gateway.on_position(short_position)
#-------------------------------------------------------------------------------------------------   
def generate_timestamp(expire_after: float = 30) -> int:
    """
    :param expire_after: expires in seconds.
    :return: timestamp in milliseconds
    """
    return int(time() * 1000 + expire_after * 1000)
#-------------------------------------------------------------------------------------------------   
def sign(secret: bytes, data: bytes) -> str:
    """
    secret签名
    """
    return hmac.new(
        secret, data, digestmod=hashlib.sha256
    ).hexdigest()