/*
  l2bot HID-мост для Arduino Leonardo / Micro / Pro Micro (чип ATmega32U4).

  Зачем: после апдейта игра перестала принимать синтетический ввод (SendInput).
  Arduino с нативным USB-HID шлёт нажатия как НАСТОЯЩАЯ клавиатура/мышь, поэтому
  игра их принимает. Питон-бот шлёт этому скетчу построчные команды по USB Serial
  (115200), а скетч выполняет их через Keyboard/Mouse.

  Протокол (одна команда = одна строка, оканчивается '\n'):
    PING                 -> отвечает "PONG" (проверка, что прошита наша прошивка)
    KEY <name>           -> нажать+отпустить клавишу. <name>:
                              один печатный символ: 2 4 a b ...
                              f1..f12
                              enter esc space tab backspace delete
                              up down left right ctrl shift alt
    MOVE <dx> <dy> <ms>   -> плавно сдвинуть мышь на (dx,dy) за ms мс (относительно)
    CLICK                 -> левый клик
    RCLICK                -> правый клик
    LDOWN / LUP           -> зажать / отпустить левую кнопку
    RDOWN / RUP           -> зажать / отпустить правую кнопку
    DRAG <dx> <dy> <ms>   -> зажать ПКМ, плавно сдвинуть, отпустить (поворот камеры)

  При загрузке печатает "L2HID" — питон по этому опознаёт правильную прошивку.
*/
#include <Keyboard.h>
#include <Mouse.h>

String command;

void setup() {
  Serial.begin(115200);
  Keyboard.begin();
  Mouse.begin();
  randomSeed(analogRead(A0));
  command.reserve(80);
  delay(300);
  Serial.println("L2HID");           // сигнал: наша прошивка загрузилась
}

// Человекоподобное относительное перемещение мыши (дуга + плавный разгон).
void smoothMove(int totalX, int totalY, int durationMs) {
  durationMs = constrain(durationMs, 40, 1500);
  int steps = constrain(durationMs / 12, 4, 90);
  int sentX = 0, sentY = 0;
  float distance = sqrt((float)totalX * totalX + (float)totalY * totalY);
  float curve = (float)random(-4, 5);
  for (int step = 1; step <= steps; step++) {
    float t = (float)step / (float)steps;
    float eased = t * t * (3.0f - 2.0f * t);           // плавный разгон/торможение
    float arc = sin(PI * t) * curve;
    float offsetX = distance > 0 ? (-totalY / distance) * arc : 0;
    float offsetY = distance > 0 ? ( totalX / distance) * arc : 0;
    int targetX = round(totalX * eased + offsetX);
    int targetY = round(totalY * eased + offsetY);
    int moveX = constrain(targetX - sentX, -127, 127);
    int moveY = constrain(targetY - sentY, -127, 127);
    Mouse.move(moveX, moveY, 0);
    sentX += moveX;
    sentY += moveY;
    delay(durationMs / steps + random(0, 4));
  }
}

// Перевести имя клавиши в HID-код. 0 = не распознано.
int keyCode(const String& n) {
  if (n.length() == 1) return (uint8_t)n[0];          // печатный символ: 2,4,a,...
  if ((n[0] == 'f' || n[0] == 'F') && n.length() <= 3) {
    int num = n.substring(1).toInt();
    if (num >= 1 && num <= 12) return KEY_F1 + (num - 1);
  }
  if (n == "enter" || n == "return") return KEY_RETURN;
  if (n == "esc") return KEY_ESC;
  if (n == "space") return ' ';
  if (n == "tab") return KEY_TAB;
  if (n == "backspace") return KEY_BACKSPACE;
  if (n == "delete") return KEY_DELETE;
  if (n == "up") return KEY_UP_ARROW;
  if (n == "down") return KEY_DOWN_ARROW;
  if (n == "left") return KEY_LEFT_ARROW;
  if (n == "right") return KEY_RIGHT_ARROW;
  if (n == "ctrl") return KEY_LEFT_CTRL;
  if (n == "shift") return KEY_LEFT_SHIFT;
  if (n == "alt") return KEY_LEFT_ALT;
  return 0;
}

void pressKey(const String& name) {
  int code = keyCode(name);
  if (code == 0) return;
  Keyboard.press((uint8_t)code);
  delay(random(40, 85));                // человекоподобное удержание
  Keyboard.releaseAll();
}

void handle(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  if (cmd == "PING") {
    Serial.println("PONG");
  } else if (cmd.startsWith("KEY ")) {
    pressKey(cmd.substring(4));
  } else if (cmd == "CLICK") {
    Mouse.click(MOUSE_LEFT);
  } else if (cmd == "RCLICK") {
    Mouse.click(MOUSE_RIGHT);
  } else if (cmd == "LDOWN") {
    Mouse.press(MOUSE_LEFT);
  } else if (cmd == "LUP") {
    Mouse.release(MOUSE_LEFT);
  } else if (cmd == "RDOWN") {
    Mouse.press(MOUSE_RIGHT);
  } else if (cmd == "RUP") {
    Mouse.release(MOUSE_RIGHT);
  } else if (cmd.startsWith("MOVE ")) {
    int dx, dy, ms;
    if (sscanf(cmd.c_str(), "MOVE %d %d %d", &dx, &dy, &ms) == 3) smoothMove(dx, dy, ms);
  } else if (cmd.startsWith("DRAG ")) {
    int dx, dy, ms;
    if (sscanf(cmd.c_str(), "DRAG %d %d %d", &dx, &dy, &ms) == 3) {
      Mouse.press(MOUSE_RIGHT);
      delay(80);
      smoothMove(dx, dy, ms);
      delay(80);
      Mouse.release(MOUSE_RIGHT);
    } else {
      Mouse.release(MOUSE_RIGHT);
    }
  }
  Serial.println("OK");               // подтверждение выполнения (для синхронизации)
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handle(command);
      command = "";
    } else if (c != '\r' && command.length() < 79) {
      command += c;
    }
  }
}
