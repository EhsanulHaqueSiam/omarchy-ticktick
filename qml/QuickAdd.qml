import QtQuick
import qs.Commons
import qs.Ui

// Natural-language capture with a live read-back of what the helper understood.
//
// `preview` is a BINDING on Service.parsePreview, not an onTextChanged call:
// the helper answers on a 250 ms debounce, and parsePreview only reaches the
// screen because reading it inside a binding registers the dependency that
// re-fires when the answer lands.
Item {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property bool hasCursor: false

  readonly property bool editing: field.activeFocus
  readonly property string text: field.text
  readonly property string preview: root.service ? root.service.parsePreview(field.text) : ""

  signal submitted(string text)
  signal dismissed()

  function focusField() {
    field.forceActiveFocus()
    field.selectAll()
  }

  function clear() {
    field.text = ""
  }

  implicitHeight: column.implicitHeight
  height: implicitHeight

  Column {
    id: column
    width: parent.width
    spacing: Style.spacing.labelGap

    TextField {
      id: field
      width: parent.width
      placeholderText: "Add a task — tomorrow 5pm !high #work"
      foreground: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      hasCursor: root.hasCursor

      onAccepted: {
        var value = field.text
        field.text = ""
        root.submitted(value)
      }

      Keys.onEscapePressed: {
        field.text = ""
        root.dismissed()
      }
    }

    Text {
      visible: root.preview !== ""
      width: parent.width
      text: root.preview
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }
}
