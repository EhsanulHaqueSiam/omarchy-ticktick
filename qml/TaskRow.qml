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
  // What a finished task's date means is when it was finished, not when it was due.
  // `completedLabel` only exists on rows from the completed endpoint, so the due
  // date still stands in for anything else that reads as done.
  readonly property string dueText: root.done
    ? (Model.squish(row.completedLabel) || Model.squish(row.dueLabel))
    : Model.squish(row.dueLabel)
  readonly property string progressText: Model.checklistProgress(row.itemsDone, row.itemsTotal)
  readonly property int priority: Model.toInt(row.priority)
  readonly property bool overdue: Model.squish(row.bucket).toLowerCase() === "overdue"
  readonly property color dim: Qt.darker(foreground, 1.5)
  // A note is something to read. TickTick gives it no checkbox and no completion,
  // so the row shows a page glyph and the click that would tick it does nothing.
  readonly property bool note: Model.isNote(row)
  // Finished work — the Done view paints it with this same delegate.
  readonly property bool done: Model.isDone(row)
  // Only an open to-do has anything to tick. A note cannot be completed and a
  // finished task cannot be reopened: the Open API cannot even look one up again.
  readonly property bool checkable: !note && !done

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
    // The box says what the task IS, never what the cursor would do to it. It used to
    // tick itself under the cursor as a preview of Enter, which is indistinguishable
    // from a task that is actually finished — every row you pointed at read as done.
    text: root.note ? "󰎞" : (root.done ? "󰄲" : "󰄱")
    // The cursor sharpens the empty box instead: same glyph, more contrast, which is
    // an affordance rather than a claim about the task.
    color: root.checkable && root.hasCursor ? root.foreground : root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.subtitle

    MouseArea {
      anchors.fill: parent
      anchors.margins: -Style.spacing.sm
      hoverEnabled: true
      enabled: root.checkable
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
    // Struck through and stepped back, the same way a ticked checklist item reads in
    // the detail pane. Done work is still worth seeing; it is not worth reading twice.
    color: root.done ? root.dim : root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    font.strikeout: root.done
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
      // Always holds its slot and fades instead of appearing: a Row drops an invisible
      // child's width AND its spacing, so revealing this on hover slid the due date,
      // the priority dot and the chip left the moment the pointer crossed the row —
      // under a pointer that was aiming at one of them.
      width: Style.space(16)
      height: Style.space(16)
      anchors.verticalCenter: parent.verticalCenter
      opacity: root.hasCursor ? 1 : 0

      Behavior on opacity {
        NumberAnimation { duration: 80 }
      }

      Text {
        anchors.centerIn: parent
        text: "󰅙"
        // Red only under its own pointer. Burning urgent on every row you happen to
        // hover reads as an alarm about the task rather than an offer to delete it,
        // and it out-shouted the overdue dates that are the actual warning.
        color: deleteHover.containsMouse ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      MouseArea {
        id: deleteHover
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onContainsMouseChanged: if (containsMouse) root.hovered(true)
        onClicked: root.deleteRequested()
      }
    }
  }
}
