import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The body that unfolds under an expanded TaskRow: description, tickable
// checklist items, repeat/reminder labels, a project picker, and whatever
// timestamps the payload carries. Everything is optional, so the whole item
// collapses to zero height when a task has nothing more to say.
Item {
  id: root

  property var task: null
  property var projects: []
  property bool expanded: false
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  signal itemToggled(string itemId, bool done)
  signal moveRequested(string projectId)

  // The panel freezes its key catcher on this: the dropdown's popup owns
  // j/k/Enter while it is open.
  readonly property bool pickerOpen: projectPicker.popupOpen

  readonly property var row: task || ({})
  readonly property string description: Model.squish(row.content) || Model.squish(row.desc)
  readonly property var items: Array.isArray(row.items) ? row.items : []
  readonly property string repeatText: Model.repeatLabel(row.repeat || row.repeatFlag)
  readonly property string remindersText: Model.remindersLabel(row.reminders)
  readonly property string projectId: String(row.projectId || "")
  readonly property color dim: Qt.darker(foreground, 1.5)

  readonly property var projectOptions: {
    var out = []
    var list = Array.isArray(projects) ? projects : []
    for (var i = 0; i < list.length; i++) {
      var p = list[i] || {}
      out.push({ value: String(p.id || ""), label: Model.squish(p.name) || "Untitled" })
    }
    return out
  }

  readonly property string metaLine: {
    var parts = []
    if (repeatText !== "") parts.push(repeatText)
    if (remindersText !== "") parts.push(remindersText)
    return parts.join(" · ")
  }

  readonly property string stampLine: {
    var parts = []
    var created = stamp(row.createdTime || row.created)
    var modified = stamp(row.modifiedTime || row.modified)
    if (created !== "") parts.push("Created " + created)
    if (modified !== "") parts.push("Modified " + modified)
    return parts.join(" · ")
  }

  readonly property bool hasDetail: description !== "" || items.length > 0
    || metaLine !== "" || stampLine !== "" || projectOptions.length > 1

  // Timestamps arrive as full ISO strings with milliseconds and an offset;
  // only the calendar day is worth showing, and slicing it can't throw.
  function stamp(value) {
    var match = /^\d{4}-\d{2}-\d{2}/.exec(String(value === undefined || value === null ? "" : value))
    return match ? match[0] : ""
  }

  visible: expanded && hasDetail
  implicitHeight: visible ? body.implicitHeight + Style.spacing.md : 0
  height: implicitHeight

  // Effective visibility drops when the row collapses, and a Popup left open
  // over a row that no longer exists would keep eating keys.
  onVisibleChanged: if (!visible) projectPicker.close()

  Column {
    id: body
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.leftMargin: Style.spacing.huge
    anchors.rightMargin: Style.spacing.rowPaddingX
    spacing: Style.spacing.md

    Text {
      visible: root.description !== ""
      width: parent.width
      text: root.description
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      maximumLineCount: 6
      elide: Text.ElideRight
    }

    Column {
      id: itemsColumn
      visible: root.items.length > 0
      width: parent.width
      spacing: Style.spacing.xs

      Repeater {
        model: root.items

        delegate: Item {
          id: itemRow
          required property var modelData

          readonly property string itemId: String(modelData && modelData.id ? modelData.id : "")
          readonly property bool done: !!(modelData && modelData.done)

          width: itemsColumn.width
          implicitHeight: itemLabel.implicitHeight + Style.spacing.xs
          height: implicitHeight

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            enabled: itemRow.itemId !== ""
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: root.itemToggled(itemRow.itemId, !itemRow.done)
          }

          Text {
            id: itemGlyph
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: itemRow.done ? "󰄲" : "󰄱"
            color: itemRow.done ? root.dim : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            id: itemLabel
            anchors.left: itemGlyph.right
            anchors.leftMargin: Style.spacing.controlGap
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: Model.squish(itemRow.modelData ? itemRow.modelData.title : "") || "Untitled item"
            color: itemRow.done ? root.dim : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.strikeout: itemRow.done
            elide: Text.ElideRight
          }
        }
      }
    }

    Text {
      visible: root.metaLine !== ""
      width: parent.width
      text: root.metaLine
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Dropdown {
      id: projectPicker
      visible: root.projectOptions.length > 1
      width: parent.width
      label: "Project"
      showLabel: false
      options: root.projectOptions
      foreground: root.foreground
      fontFamily: root.fontFamily
      onChanged: function (value) { root.moveRequested(value) }
    }

    // Selecting an option assigns Dropdown.value imperatively, which would
    // otherwise strand the trigger label on the old project forever.
    Binding {
      target: projectPicker
      property: "value"
      value: root.projectId
    }

    Text {
      visible: root.stampLine !== ""
      width: parent.width
      text: root.stampLine
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }
}
