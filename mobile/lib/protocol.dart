import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:pointycastle/export.dart' as pc;

const int tJson = 1, tFrame = 2, tChunk = 3;
const int defaultPort = 7001, serverPort = 7000;
const int maxMessage = 64 * 1024 * 1024;

class AuthError implements Exception {
  final String message;
  AuthError(this.message);
  @override
  String toString() => message;
}

class ConnectError implements Exception {
  final String message;
  ConnectError(this.message);
  @override
  String toString() => message;
}

(String, int) parseAddress(String text, int defaultPortValue) {
  final t = text.trim();
  final i = t.lastIndexOf(':');
  if (i > 0) {
    final port = int.tryParse(t.substring(i + 1));
    if (port != null) return (t.substring(0, i), port);
  }
  return (t, defaultPortValue);
}

class ByteReader {
  final List<Uint8List> _chunks = [];
  int _available = 0;
  Completer<void>? _waiting;
  bool _done = false;
  Object? _error;
  late final StreamSubscription<Uint8List> _sub;

  ByteReader(Stream<Uint8List> stream) {
    _sub = stream.listen((data) {
      _chunks.add(data);
      _available += data.length;
      _wake();
    }, onError: (Object e) {
      _error = e;
      _done = true;
      _wake();
    }, onDone: () {
      _done = true;
      _wake();
    }, cancelOnError: true);
  }

  void _wake() {
    final w = _waiting;
    _waiting = null;
    w?.complete();
  }

  Future<Uint8List> read(int n) async {
    while (_available < n) {
      if (_done) throw ConnectError(_error?.toString() ?? 'connection closed');
      _waiting = Completer<void>();
      await _waiting!.future;
    }
    final out = Uint8List(n);
    var got = 0;
    while (got < n) {
      final first = _chunks.first;
      final take = min(first.length, n - got);
      out.setRange(got, got + take, first);
      got += take;
      if (take == first.length) {
        _chunks.removeAt(0);
      } else {
        _chunks[0] = Uint8List.sublistView(first, take);
      }
    }
    _available -= n;
    return out;
  }

  Future<String> readLine({int limit = 1 << 20}) async {
    final bytes = BytesBuilder(copy: false);
    while (true) {
      final b = await read(1);
      if (b[0] == 10) return utf8.decode(bytes.takeBytes());
      bytes.add(b);
      if (bytes.length > limit) throw ConnectError('line too long');
    }
  }

  Future<void> cancel() => _sub.cancel();
}

Uint8List _hmac(Uint8List key, List<List<int>> parts) {
  final mac = pc.HMac(pc.SHA256Digest(), 64)..init(pc.KeyParameter(key));
  for (final p in parts) {
    mac.update(Uint8List.fromList(p), 0, p.length);
  }
  final out = Uint8List(32);
  mac.doFinal(out, 0);
  return out;
}

Uint8List deriveKey(String password, String peerId) {
  final s = pc.Scrypt()
    ..init(pc.ScryptParameters(1 << 14, 8, 1, 32, Uint8List.fromList(utf8.encode('anykir-v1:$peerId'))));
  return s.process(Uint8List.fromList(utf8.encode(password)));
}

bool _same(List<int> a, List<int> b) {
  if (a.length != b.length) return false;
  var d = 0;
  for (var i = 0; i < a.length; i++) {
    d |= a[i] ^ b[i];
  }
  return d == 0;
}

Uint8List _random(int n) {
  final r = Random.secure();
  return Uint8List.fromList(List.generate(n, (_) => r.nextInt(256)));
}

class SecureChannel {
  final Socket socket;
  final ByteReader reader;
  final SecretKey _key;
  final List<int> _sendPrefix, _recvPrefix;
  final AesGcm _aes = AesGcm.with256bits();
  int _sc = 0, _rc = 0;
  Future<void> _sendQueue = Future.value();
  bool closed = false;

  SecureChannel(this.socket, this.reader, Uint8List sessionKey, this._sendPrefix, this._recvPrefix)
      : _key = SecretKey(sessionKey);

  List<int> _nonce(List<int> prefix, int counter) {
    final n = Uint8List(12)..setRange(0, 4, prefix);
    ByteData.sublistView(n).setUint64(4, counter, Endian.big);
    return n;
  }

  Future<void> send(int type, List<int> body) {
    final nonce = _nonce(_sendPrefix, _sc++);
    final plain = Uint8List(body.length + 1)
      ..[0] = type
      ..setRange(1, body.length + 1, body);
    _sendQueue = _sendQueue.then((_) async {
      if (closed) return;
      final box = await _aes.encrypt(plain, secretKey: _key, nonce: nonce);
      final ct = Uint8List(box.cipherText.length + 16)
        ..setRange(0, box.cipherText.length, box.cipherText)
        ..setRange(box.cipherText.length, box.cipherText.length + 16, box.mac.bytes);
      final head = ByteData(4)..setUint32(0, ct.length, Endian.big);
      socket.add(head.buffer.asUint8List());
      socket.add(ct);
    }).catchError((_) {});
    return _sendQueue;
  }

  Future<void> sendJson(Map<String, Object?> obj) => send(tJson, utf8.encode(jsonEncode(obj)));

  Future<(int, Uint8List)> recv() async {
    final head = await reader.read(4);
    final n = ByteData.sublistView(head).getUint32(0, Endian.big);
    if (n < 17 || n > maxMessage) throw ConnectError('bad message size');
    final ct = await reader.read(n);
    final nonce = _nonce(_recvPrefix, _rc++);
    final box = SecretBox(Uint8List.sublistView(ct, 0, n - 16),
        nonce: nonce, mac: Mac(Uint8List.sublistView(ct, n - 16)));
    final plain = Uint8List.fromList(await _aes.decrypt(box, secretKey: _key));
    return (plain[0], Uint8List.sublistView(plain, 1));
  }

  void close() {
    closed = true;
    try {
      socket.destroy();
    } catch (_) {}
  }
}

Future<Socket> openSocket(String host, int port, {Duration timeout = const Duration(seconds: 10)}) async {
  final s = await Socket.connect(host, port, timeout: timeout);
  s.setOption(SocketOption.tcpNoDelay, true);
  return s;
}

Future<SecureChannel> clientHandshake(Socket socket, ByteReader reader, String password) async {
  final nv = _random(16);
  socket.add([...ascii.encode('AK1'), ...nv]);
  final idLen = (await reader.read(1))[0];
  final peerId = ascii.decode(await reader.read(idLen));
  final data = await reader.read(48);
  final nh = Uint8List.sublistView(data, 0, 16);
  final proof = Uint8List.sublistView(data, 16);
  final key = await Isolate.run(() => deriveKey(password, peerId));
  if (!_same(proof, _hmac(key, [ascii.encode('host'), nv, nh]))) {
    throw AuthError('Wrong password or ID');
  }
  socket.add(_hmac(key, [ascii.encode('viewer'), nh, nv]));
  final session = _hmac(key, [ascii.encode('session'), nv, nh]);
  return SecureChannel(socket, reader, session, [86, 0, 0, 0], [72, 0, 0, 0]);
}

Future<SecureChannel> hostHandshake(Socket socket, ByteReader reader, Uint8List key, String hostId) async {
  final hello = await reader.read(19);
  if (hello[0] != 0x41 || hello[1] != 0x4B || hello[2] != 0x31) throw AuthError('Not an AnyKir connection');
  final nv = Uint8List.sublistView(hello, 3);
  final nh = _random(16);
  final ident = ascii.encode(hostId);
  socket.add([ident.length, ...ident, ...nh, ..._hmac(key, [ascii.encode('host'), nv, nh])]);
  final proof = await reader.read(32);
  if (!_same(proof, _hmac(key, [ascii.encode('viewer'), nh, nv]))) throw AuthError('Wrong password');
  return SecureChannel(socket, reader, _hmac(key, [ascii.encode('session'), nv, nh]), [72, 0, 0, 0], [86, 0, 0, 0]);
}

class OnlinePc {
  final String id, name, peer, version;
  final List<String> ips;
  final int port;
  final bool admin;
  OnlinePc(Map<String, dynamic> m)
      : id = '${m['id']}',
        name = '${m['name'] ?? ''}',
        peer = '${m['peer'] ?? ''}',
        version = '${m['ver'] ?? ''}',
        ips = [for (final x in (m['ips'] as List? ?? const [])) '$x'],
        port = (m['port'] as int?) ?? defaultPort,
        admin = m['admin'] == true;
  String get address => '$peer:$port';

  String? get tailscaleAddress {
    for (final ip in [peer, ...ips]) {
      if (isTailscaleIp(ip)) return '$ip:$port';
    }
    return null;
  }
}

Future<List<OnlinePc>> fetchOnline(String server) async {
  final (host, port) = parseAddress(server, serverPort);
  final s = await openSocket(host, port, timeout: const Duration(seconds: 3));
  final r = ByteReader(s);
  try {
    s.add(utf8.encode('${jsonEncode({'op': 'list'})}\n'));
    final reply = jsonDecode(await r.readLine().timeout(const Duration(seconds: 3))) as Map<String, dynamic>;
    if (reply['ok'] != true) return [];
    final list = [for (final h in reply['hosts'] as List) OnlinePc(h as Map<String, dynamic>)];
    list.sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    return list;
  } finally {
    s.destroy();
  }
}

Future<(Socket, ByteReader)> connectVia(String server, String pcId) async {
  final (host, port) = parseAddress(server, serverPort);
  final s = await openSocket(host, port, timeout: const Duration(seconds: 5));
  final r = ByteReader(s);
  try {
    s.add(utf8.encode('${jsonEncode({'op': 'connect', 'id': pcId})}\n'));
    final reply = jsonDecode(await r.readLine().timeout(const Duration(seconds: 15))) as Map<String, dynamic>;
    if (reply['ok'] != true) throw ConnectError('${reply['err'] ?? 'the server refused'}');
    return (s, r);
  } catch (e) {
    s.destroy();
    rethrow;
  }
}

OnlinePc? matchPc(List<OnlinePc> pcs, String address) {
  final wanted = parseAddress(address, defaultPort).$1.trim().toLowerCase();
  if (wanted.isEmpty) return null;
  for (final p in pcs) {
    if (wanted == p.peer || p.ips.contains(wanted) || wanted == p.name.toLowerCase()) return p;
  }
  return null;
}

bool isTailscaleIp(String ip) {
  final parts = ip.split('.');
  if (parts.length != 4) return false;
  final a = int.tryParse(parts[0]), b = int.tryParse(parts[1]);
  return a == 100 && b != null && b >= 64 && b <= 127;
}

Future<String?> phoneTailscaleIp() async {
  try {
    for (final nic in await NetworkInterface.list(type: InternetAddressType.IPv4)) {
      for (final a in nic.addresses) {
        if (isTailscaleIp(a.address)) return a.address;
      }
    }
  } catch (_) {}
  return null;
}

List<String> splitServers(String text) =>
    [for (final s in text.split(RegExp(r'[,;\s]+'))) if (s.trim().isNotEmpty) s.trim()];

Future<(String, List<OnlinePc>)> fetchOnlineAny(List<String> servers) async {
  Object? last;
  for (final s in servers) {
    try {
      return (s, await fetchOnline(s));
    } catch (e) {
      last = e;
    }
  }
  throw last ?? ConnectError('No AnyKir Server set');
}
