import logging
import azure.functions as func

def main(msg: func.ServiceBusMessage):
    body = msg.get_body().decode("utf-8")
    logging.info(f"Received: {body}")
