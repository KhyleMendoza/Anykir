import 'dart:convert';
import 'dart:io';

import 'package:anykir_mobile/updates.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('reached feed vs no feed', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((r) {
      r.response.write(jsonEncode({'version': '1.0.0', 'sig': 'x', 'file': 'AnyKir.apk', 'size': 1}));
      r.response.close();
    });
    try {
      expect(await checkForUpdate(sources: ['http://127.0.0.1:${server.port}'], current: '1.51.8'), isNull);
      expect(feedReached, isTrue, reason: 'answered: really up to date');
      expect(await checkForUpdate(sources: ['http://127.0.0.1:1'], current: '1.51.8'), isNull);
      expect(feedReached, isFalse, reason: 'nothing answered: must not claim up to date');
    } finally {
      await server.close(force: true);
    }
  });
}
