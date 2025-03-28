import time
import datetime
import random


def simulate_delay(seconds):
    time.sleep(seconds)


def simulate_random_failure(probability=0.1):
    if random.random() < probability:
        return True
    return False


def get_current_time():
    return datetime.datetime.now()
