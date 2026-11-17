import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:anykir_mobile/protocol.dart';

Future<void> main(List<String> args) async {
  final server = args[0], direct = args[1], password = args[2];

  final online = await fetchOnline(server);
  print('server lists: ${online.map((p) => '${p.name} ${p.address}').join(', ')}');
  final pc = online.first;

  try {
    final (sock, reader) = await connectVia(server, pc.id);
    await clientHandshake(sock, reader, 'wrong-password');
    print('FAIL: wrong password accepted');
    exit(1);
  } on AuthError catch (e) {
    print('wrong password rejected: $e');
  }
  await Future<void>.delayed(const Duration(milliseconds: 1500));

  for (final route in ['server', 'direct']) {
    Socket sock;
    ByteReader reader;
    if (route == 'server') {
      (sock, reader) = await connectVia(server, pc.id);
    } else {
      final (h, p) = parseAddress(direct, defaultPort);
      sock = await openSocket(h, p);
      reader = ByteReader(sock);
    }
    final t0 = DateTime.now();
    final ch = await clientHandshake(sock, reader, password);
    print('[$route] logged in after ${DateTime.now().difference(t0).inMilliseconds} ms');
    await ch.sendJson({'t': 'caps', 'ack': 1});
    await ch.sendJson({'t': 'cfg', 'quality': 50, 'scale': 0.6, 'fps': 20});
    var frames = 0, bytes = 0, rx = 0;
    String? hello, screen;
    final until = DateTime.now().add(const Duration(seconds: 4));
    while (DateTime.now().isBefore(until)) {
      final (type, body) = await ch.recv();
      if (type == tFrame) {
        rx++;
        await ch.sendJson({'t': 'ack', 'n': rx});
        final head = ByteData.sublistView(body, 0, 8);
        final jpeg = Uint8List.sublistView(body, 8);
        if (frames == 0) print('[$route] first frame tile ${head.getUint16(4)}x${head.getUint16(6)}, JPEG ${jpeg[0] == 0xFF && jpeg[1] == 0xD8}');
        frames++;
        bytes += body.length;
      } else if (type == tJson) {
        final m = jsonDecode(utf8.decode(body)) as Map<String, dynamic>;
        if (m['t'] == 'hello') hello = 'host=${m['host']} ver=${m['ver']} input=${m['input']}';
        if (m['t'] == 'screen') screen = '${m['w']}x${m['h']}';
      }
    }
    await ch.sendJson({'t': 'mouse', 'a': 'move', 'x': 0.5, 'y': 0.5});
    print('[$route] hello: $hello | screen: $screen | frames in 4 s: $frames (${bytes ~/ 1024} KB), all acknowledged');
    ch.close();
    await Future<void>.delayed(const Duration(milliseconds: 1500));
  }
  print('ALL OK');
  exit(0);
}
