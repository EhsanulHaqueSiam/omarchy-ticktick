import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// One task line: checkbox · title · project chip · checklist progress ·
// priority dot · due label, plus a delete affordance revealed under the cursor.
//
// Every field on `task` is read through Model's total helpers. views.collect
// always emits the full row shape, but an optimistic local patch or a payload
// from an older helper must still paint a row — a blank widget is a worse
// failure than a row that renders "".
CursorSurface {
  id: root

  property var task: null
  property bool expanded: false
  property bool showProjectChip: true
  property string fontFamily: Style.font.family
  property color urgent: Color.urgent

  signal completeRequested()
  signal deleteRequested()
  signal expandRequested()
  signal priorityCycleRequested()
  signal hovered(bool isHovered)

  readonly property var row: task || ({})
  readonly property string titleText: Model.squish(row.title) || "Untitled task"
  readonly property string projectName: Model.squish(row.project)
  readonly property string dueText: Model.squish(row.dueLabel)
  readonly property string progressText: Model.checklistProgress(row.itemsDone, row.itemsTotal)
  readonly property int priority: Model.toInt(row.priority)
  readonly property bool overdue: Model.squish(row.bucket).toLowerCase() === "overdue"
  readonly property color dim: Qt.darker(foreground, 1.5)
  // A note is something to read. TickTick gives it no checkbox and no completion,
  // so the row shows a page glyph and the click that would tick it does nothing.
  readonly property bool note: Model.isNote(row)

  // A project colour comes from the account, so it can be anything or nothing.
  // Anything that is not an obvious hex falls back to the theme rather than
  // handing Qt.color() a string it will warn about.
  readonly property color chipColor: /^#[0-9a-fA-F]{3,8}$/.test(String(row.projectColor || ""))
    ? Qt.color(String(row.projectColor))
    : dim

  // The open row keeps the "current" tint so the detail underneath reads as
  // belonging to it even after the cursor walks away.
  current: expanded
  implicitHeight: Math.max(check.implicitHeight, title.implicitHeight, trailing.implicitHeight) + Style.spacing.xl

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onContainsMouseChanged: root.hovered(containsMouse)
    onClicked: root.expandRequested()
  }

  Text {
    id: check
    anchors.left: parent.left
    anchors.leftMargin: Style.spacing.rowPaddingX
    anchors.verticalCenter: parent.verticalCenter
    // Ticks under the cursor so the row previews what Enter / a click does — but a
    // note has nothing to preview, so it keeps its own glyph throughout.
    text: root.note ? "󰎞" : (root.hasCursor ? "󰄲" : "󰄱")
    color: root.note ? root.dim : (root.hasCursor ? root.foreground : root.dim)
    font.family: root.fontFamily
    font.pixelSize: Style.font.subtitle

    MouseArea {
      anchors.fill: parent
      anchors.margins: -Style.spacing.sm
      hoverEnabled: true
      enabled: !root.note
      cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onContainsMouseChanged: if (containsMouse) root.hovered(true)
      onClicked: root.completeRequested()
    }
  }

  Text {
    id: title
    anchors.left: check.right
    anchors.leftMargin: Style.spacing.controlGap
    anchors.right: trailing.left
    anchors.rightMargin: Style.spacing.md
    anchors.verticalCenter: parent.verticalCenter
    text: root.titleText
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    elide: Text.ElideRight
  }

  Row {
    id: trailing
    anchors.right: parent.right
    anchors.rightMargin: Style.spacing.rowPaddingX
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.spacing.md

    BorderSurface {
      id: chip
      visible: root.showProjectChip && root.projectName !== ""
      width: visible ? chipText.implicitWidth + Style.spacing.lg : 0
      height: chipText.implicitHeight + Style.spacing.xs
      anchors.verticalCenter: parent.verticalCenter
      color: "transparent"
      radius: Style.cornerRadius
      borderSpec: Border.controlSpec("normal", root.chipColor, root.chipColor)

      Text {
        id: chipText
        anchors.centerIn: parent
        text: Model.elide(root.projectName, 14)
        textFormat: Text.PlainText
        color: root.chipColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Text {
      id: progress
      visible: root.progressText !== ""
      anchors.verticalCenter: parent.verticalCenter
      text: root.progressText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Item {
      id: priorityDot
      visible: root.priority > 0
      width: visible ? Style.space(12) : 0
      height: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter

      Text {
        anchors.centerIn: parent
        text: "●"
        color: Model.priorityColor(root.priority, root.foreground, root.urgent)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onContainsMouseChanged: if (containsMouse) root.hovered(true)
        onClicked: root.priorityCycleRequested()
      }
    }

    Text {
      id: due
      visible: root.dueText !== ""
      anchors.verticalCenter: parent.verticalCenter
      text: root.dueText
      color: root.overdue ? root.urgent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Item {
      id: deleteAction
      visible: root.hasCursor
      width: visible ? Style.space(16) : 0
      height: Style.space(16)
      anchors.verticalCenter: parent.verticalCenter

      Text {
        anchors.centerIn: parent
        text: "󰅙"
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onContainsMouseChanged: if (containsMouse) root.hovered(true)
        onClicked: root.deleteRequested()
      }
    }
  }
}
