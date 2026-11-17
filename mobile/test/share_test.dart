import 'dart:io';

import 'package:anykir_mobile/host.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final hub = Platform.environment['ANYKIR_HUB'], lan = Platform.environment['ANYKIR_LAN'];
  test('share on one address', () async {
    final host = PhoneHost(id: '987654321', password: 'test-password-1', name: 'TestPhone', servers: ['127.0.0.1:${hub!.split(':').last}'],
        onlyAddress: lan);
    await host.start();
    try {
      await Future<void>.delayed(const Duration(seconds: 4));
      print('status: ${host.status.value}');
      Future<bool> reach(String ip) async {
        try {
          final s = await Socket.connect(ip, 7001, timeout: const Duration(seconds: 2));
          s.destroy();
          return true;
        } catch (_) {
          return false;
        }
      }
      print('direct on $lan: ${await reach(lan!)}, on 127.0.0.1: ${await reach('127.0.0.1')}');
      expect(await reach(lan), isTrue);
      expect(await reach('127.0.0.1'), isFalse);
      expect(host.status.value, contains('Online'));
      await Future<void>.delayed(const Duration(seconds: 12));
    } finally {
      try {
        await host.stop();
      } catch (_) {}
    }
  }, skip: hub == null || lan == null, timeout: const Timeout(Duration(seconds: 60)));
}
