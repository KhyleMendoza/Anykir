import 'package:anykir_mobile/session.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final t0 = DateTime(2026, 11, 7, 12);
  DateTime at(int s) => t0.add(Duration(seconds: s));

  test('Wi-Fi: every 10 s while the screen changes', () {
    expect(healDue(at(9), t0, at(8), metered: false), isFalse, reason: 'too soon');
    expect(healDue(at(10), t0, at(8), metered: false), isTrue);
  });

  test('mobile data: every 45 s', () {
    expect(healDue(at(30), t0, at(29), metered: true), isFalse);
    expect(healDue(at(45), t0, at(44), metered: true), isTrue);
  });

  test('still screen: never', () {
    expect(healDue(at(600), t0, at(1), metered: false), isFalse, reason: 'only the refresh itself arrived');
    expect(healDue(at(600), t0, DateTime(0), metered: false), isFalse);
  });
}
