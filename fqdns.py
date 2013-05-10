import argparse
import socket
import logging
import sys
import os
import select
import contextlib
import time
import struct

import dpkt

import gevent.server
import gevent.monkey


LOGGER = logging.getLogger(__name__)


class DNSServer(gevent.server.DatagramServer):
    max_wait = 1
    max_retry = 2
    max_cache_size = 20000
    timeout = 6

    def handle(self, data, address):
        pass


def serve():
    pass


def resolve(domain, server_type, at, timeout, strategy, wrong_answer):
    server_ip, server_port = parse_at(at)
    LOGGER.info('resolve %s at %s:%s' % (domain, server_ip, server_port))
    if 'udp' == server_type:
        return resolve_over_udp(
            domain, server_ip, server_port, timeout,
            strategy, set(wrong_answer) if wrong_answer else set())
    elif 'tcp' == server_type:
        return resolve_over_tcp(domain, server_ip, server_port, timeout)
    else:
        raise Exception('unsupported server type: %s' % server_type)


def parse_at(at):
    if ':' in at:
        server_ip, server_port = at.split(':')
        server_port = int(server_port)
    else:
        server_ip = at
        server_port = 53
    return server_ip, server_port


def resolve_over_tcp(domain, server_ip, server_port, timeout):
    sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM)
    sock.settimeout(timeout)
    with contextlib.closing(sock):
        request = dpkt.dns.DNS(id=os.getpid(), qd=[dpkt.dns.DNS.Q(name=domain, type=dpkt.dns.DNS_A)])
        LOGGER.info('send request: %s' % repr(request))
        sock.connect((server_ip, server_port))
        data = str(request)
        sock.send(struct.pack('>h', len(data)) + data)
        rfile = sock.makefile('r', 512)
        data = rfile.read(2)
        data = rfile.read(struct.unpack('>h', data)[0])
        response = dpkt.dns.DNS(data)
        if response:
            return list_ipv4_addresses(response)
        else:
            return []


def resolve_over_udp(domain, server_ip, server_port, timeout, strategy, wrong_answers):
    sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    sock.settimeout(1)
    with contextlib.closing(sock):
        request = dpkt.dns.DNS(id=os.getpid(), qd=[dpkt.dns.DNS.Q(name=domain, type=dpkt.dns.DNS_A)])
        LOGGER.info('send request: %s' % repr(request))
        sock.sendto(str(request), (server_ip, server_port))
        responses = pick_responses(sock, timeout, strategy, wrong_answers)

        if len(responses) == 1:
            return list_ipv4_addresses(responses[0])
        elif len(responses) > 1:
            return [list_ipv4_addresses(response) for response in responses]
        else:
            return []


def pick_responses(sock, timeout, strategy, wrong_answers):
    picked_responses = []
    started_at = time.time()
    remaining_timeout = started_at + timeout - time.time()
    while remaining_timeout > 0:
        LOGGER.info('wait for max %s seconds' % remaining_timeout)
        ins, outs, errors = select.select([sock], [], [sock], remaining_timeout)
        if errors:
            raise Exception('failed to read dns response')
        if not ins:
            return picked_responses
        response = dpkt.dns.DNS(sock.recv(512))
        LOGGER.info('received response: %s' % repr(response))
        if 'pick-first' == strategy:
            return response
        elif 'pick-later' == strategy:
            picked_responses = [response]
        elif 'pick-right' == strategy:
            if is_right_response(response, wrong_answers):
                return response
        elif 'pick-right-later' == strategy:
            if is_right_response(response, wrong_answers):
                picked_responses = [response]
        elif 'pick-all' == strategy:
            picked_responses.append(response)
        else:
            raise Exception('unsupported strategy: %s' % strategy)
        remaining_timeout = started_at + timeout - time.time()
    return picked_responses


def is_right_response(response, wrong_answers):
    answers = list_ipv4_addresses(response)
    if not answers: # GFW can forge empty response
        return False
    if len(answers) > 1: # GFW does not forge response with more than one answer
        return True
    return not any(answer in wrong_answers for answer in answers)


def list_ipv4_addresses(response):
    return [socket.inet_ntoa(answer.rdata) for answer in response.an if dpkt.dns.DNS_A == answer.type]


def discover(domain, at, timeout):
    server_ip, server_port = parse_at(at)
    domains = domain
    wrong_answers = set()
    for domain in domains:
        wrong_answers |= discover_once(domain, server_ip, server_port, timeout)
    return wrong_answers


def discover_once(domain, server_ip, server_port, timeout):
    wrong_answers = set()
    responses_answers = resolve_over_udp(domain, server_ip, server_port, timeout, 'pick-all', set())
    contains_right_answer = any(len(answers) > 1 for answers in responses_answers)
    if contains_right_answer:
        for answers in responses_answers:
            if len(answers) == 1:
                wrong_answers |= set(answers)
    return wrong_answers

# TODO multiple --at
# TODO --recursive
# TODO multiple domain
# TODO concurrent query
# TODO pick-right pick-right-later with multiple --wrong-answer
# TODO --auto-discover-wrong-answers
# TODO --record-type

if '__main__' == __name__:
    gevent.monkey.patch_all(dns=gevent.version_info[0] >= 1)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    argument_parser = argparse.ArgumentParser()
    sub_parsers = argument_parser.add_subparsers()
    resolve_parser = sub_parsers.add_parser('resolve', help='start as dns client')
    resolve_parser.add_argument('domain')
    resolve_parser.add_argument('--at', help='dns server', default='8.8.8.8:53')
    resolve_parser.add_argument(
        '--strategy', help='anti-GFW strategy', default='pick-first',
        choices=['pick-first', 'pick-later', 'pick-right', 'pick-right-later', 'pick-all'])
    resolve_parser.add_argument('--wrong-answer', help='wrong answer forged by GFW', nargs='*')
    resolve_parser.add_argument('--timeout', help='in seconds', default=1, type=float)
    resolve_parser.add_argument('--server-type', default='udp', choices=['udp', 'tcp'])
    resolve_parser.set_defaults(handler=resolve)
    discover_parser = sub_parsers.add_parser('discover', help='resolve black listed domain to discover wrong answers')
    discover_parser.add_argument('--at', help='dns server', default='8.8.8.8:53')
    discover_parser.add_argument('--timeout', help='in seconds', default=1, type=float)
    discover_parser.add_argument('domain', nargs='+', help='black listed domain such as twitter.com')
    discover_parser.set_defaults(handler=discover)
    serve_parser = sub_parsers.add_parser('serve', help='start as dns server')
    serve_parser.set_defaults(handler=serve)
    args = argument_parser.parse_args()
    sys.stderr.write(repr(args.handler(**{k: getattr(args, k) for k in vars(args) if k != 'handler'})))
    sys.stderr.write('\n')