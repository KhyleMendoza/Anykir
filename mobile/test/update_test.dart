import 'dart:io';

import 'package:anykir_mobile/updates.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('version compare', () {
    expect(isNewer('1.1.22', '1.1.21'), isTrue);
    expect(isNewer('1.2.0', '1.1.99'), isTrue);
    expect(isNewer('1.1.21', '1.1.21'), isFalse);
    expect(isNewer('1.0.9', '1.1.0'), isFalse);
    expect(isNewer('bad', '1.0.0'), isFalse);
  });

  final feed = Platform.environment['ANYKIR_FEED'];
  test('signed update is found, downloaded and verified', () async {
    final u = await checkForUpdate(sources: ['http://127.0.0.1:1', feed!], current: '1.0.0');
    expect(u, isNotNull);
    expect(await checkForUpdate(sources: [feed], current: u!.version), isNull);
    final dir = await Directory.systemTemp.createTemp('anykir_upd');
    final path = await downloadUpdate(u, dir: dir.path);
    expect(File(path).existsSync(), isTrue);
    final tampered = AppUpdate(u.version, u.sig, u.file, u.size, u.source);
    final bad = AppUpdate('9.9.9', tampered.sig, tampered.file, tampered.size, tampered.source);
    await expectLater(downloadUpdate(bad, dir: dir.path), throwsA(isA<Exception>()));
    await dir.delete(recursive: true);
  }, skip: feed == null ? 'set ANYKIR_FEED' : false);
}
