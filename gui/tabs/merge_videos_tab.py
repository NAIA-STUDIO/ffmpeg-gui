# gui/tabs/merge_videos_tab.py
"""
MergeVideosTab: Pestaña para unir varios videos en uno solo.

Incluye dos flujos:
1) Unión manual de una lista de vídeos.
2) Emparejado automático entre dos carpetas por resolución y variante 'sin logo'.

Pensada para campañas con muchos clips, permitiendo:
- Seleccionar múltiples vídeos.
- Reordenarlos.
- Arrastrarlos y soltarlos.
- Elegir entre:
    1) Unión rápida sin recodificar.
    2) Unión compatible recodificando.
- Seleccionar dos carpetas y fusionar automáticamente sólo los vídeos emparejables.
"""

import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox, QPushButton, QLabel, QFileDialog,
    QScrollArea, QListWidget, QListWidgetItem, QHBoxLayout, QComboBox, QLineEdit
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFontMetrics

from logic.ffmpeg_worker import FFmpegWorker
from gui.task_widget import ConversionTaskWidget
from logic.ffmpeg_logic import (
    merge_videos_command,
    pair_videos_by_resolution,
    build_auto_merge_output_name
)


class MergeVideosTab(QWidget):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

        self.input_videos = []
        self.active_workers = []

        self.folder_1_path = None
        self.folder_2_path = None

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        # =========================================================
        # Unión manual
        # =========================================================
        group_videos = QGroupBox("Unión Manual de Videos")
        videos_layout = QVBoxLayout()

        self.videos_label = QLabel("Videos seleccionados: 0")
        videos_layout.addWidget(self.videos_label)

        self.btn_add_videos = QPushButton("Añadir Videos")
        self.btn_add_videos.clicked.connect(self.select_video_files)
        videos_layout.addWidget(self.btn_add_videos)

        self.video_list_widget = QListWidget()
        self.video_list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.video_list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.video_list_widget.model().rowsMoved.connect(self.sync_input_videos_from_widget)
        videos_layout.addWidget(self.video_list_widget)

        row_buttons = QHBoxLayout()

        self.btn_remove_selected = QPushButton("Quitar Seleccionados")
        self.btn_remove_selected.clicked.connect(self.remove_selected_videos)
        row_buttons.addWidget(self.btn_remove_selected)

        self.btn_move_up = QPushButton("Subir")
        self.btn_move_up.clicked.connect(self.move_selected_up)
        row_buttons.addWidget(self.btn_move_up)

        self.btn_move_down = QPushButton("Bajar")
        self.btn_move_down.clicked.connect(self.move_selected_down)
        row_buttons.addWidget(self.btn_move_down)

        self.btn_clear_videos = QPushButton("Limpiar Lista")
        self.btn_clear_videos.clicked.connect(self.clear_videos)
        row_buttons.addWidget(self.btn_clear_videos)

        videos_layout.addLayout(row_buttons)
        group_videos.setLayout(videos_layout)
        layout.addWidget(group_videos)

        # =========================================================
        # Emparejado automático por carpetas
        # =========================================================
        group_auto = QGroupBox("Emparejado Automático por Carpetas")
        auto_layout = QVBoxLayout()

        self.folder_1_label = QLabel("Carpeta 1 (Video Campaña): no seleccionada")
        self.folder_1_label.setToolTip("")
        auto_layout.addWidget(self.folder_1_label)

        self.btn_select_folder_1 = QPushButton("Seleccionar Carpeta 1")
        self.btn_select_folder_1.clicked.connect(self.select_folder_1)
        auto_layout.addWidget(self.btn_select_folder_1)

        self.folder_2_label = QLabel("Carpeta 2 (Lookbook Campaña): no seleccionada")
        self.folder_2_label.setToolTip("")
        auto_layout.addWidget(self.folder_2_label)

        self.btn_select_folder_2 = QPushButton("Seleccionar Carpeta 2")
        self.btn_select_folder_2.clicked.connect(self.select_folder_2)
        auto_layout.addWidget(self.btn_select_folder_2)

        self.auto_hint_label = QLabel(
            "La app empareja por resolución y distingue automáticamente la variante 'sin logo' si aparece en el nombre del archivo."
        )
        self.auto_hint_label.setWordWrap(True)
        auto_layout.addWidget(self.auto_hint_label)

        self.auto_summary_label = QLabel("")
        self.auto_summary_label.setWordWrap(True)
        auto_layout.addWidget(self.auto_summary_label)

        self.btn_merge_folders = QPushButton("Fusionar Carpetas por Resolución")
        self.btn_merge_folders.clicked.connect(self.merge_folders_by_resolution)
        auto_layout.addWidget(self.btn_merge_folders)

        group_auto.setLayout(auto_layout)
        layout.addWidget(group_auto)

        # =========================================================
        # Configuración general
        # =========================================================
        group_config = QGroupBox("Configuración de Unión")
        config_layout = QVBoxLayout()

        self.mode_label = QLabel("Modo de unión:")
        config_layout.addWidget(self.mode_label)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "Rápido (sin recodificar)",
            "Compatible (recodificar)"
        ])
        self.mode_combo.currentTextChanged.connect(self.update_mode_visibility)
        config_layout.addWidget(self.mode_combo)

        self.output_name_label = QLabel("Nombre de salida manual (sin extensión, opcional):")
        config_layout.addWidget(self.output_name_label)

        self.output_name_input = QLineEdit("")
        config_layout.addWidget(self.output_name_input)

        self.preset_label = QLabel("Preset (solo modo compatible):")
        config_layout.addWidget(self.preset_label)

        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "ultrafast", "superfast", "veryfast", "faster",
            "fast", "medium", "slow", "slower", "veryslow"
        ])
        self.preset_combo.setCurrentText("slow")
        config_layout.addWidget(self.preset_combo)

        self.crf_label = QLabel("CRF (solo modo compatible):")
        config_layout.addWidget(self.crf_label)

        self.crf_input = QLineEdit("19")
        config_layout.addWidget(self.crf_input)

        group_config.setLayout(config_layout)
        layout.addWidget(group_config)

        # =========================================================
        # Botones de procesado
        # =========================================================
        row_process = QHBoxLayout()

        self.btn_merge_videos = QPushButton("Unir Lista Manual")
        self.btn_merge_videos.clicked.connect(self.merge_videos)
        row_process.addWidget(self.btn_merge_videos)

        layout.addLayout(row_process)

        # =========================================================
        # Tareas
        # =========================================================
        group_tasks = QGroupBox("Tareas de Unión")
        self.tasks_layout = QVBoxLayout()
        self.tasks_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        group_tasks.setLayout(self.tasks_layout)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumHeight(120)
        scroll_content = QWidget()
        scroll_content.setLayout(self.tasks_layout)
        scroll_area.setWidget(scroll_content)

        layout.addWidget(group_tasks)
        layout.addWidget(scroll_area)

        self.setLayout(layout)
        self.update_mode_visibility(self.mode_combo.currentText())

    # =========================================================
    # Unión manual - selección y gestión de vídeos
    # =========================================================
    def select_video_files(self):
        """Abre un diálogo para seleccionar múltiples vídeos."""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Seleccionar Videos",
            "",
            "Videos (*.mp4 *.avi *.mkv *.mov)"
        )
        if file_paths:
            self.add_videos(file_paths)

    def add_videos(self, file_paths):
        """Añade vídeos a la lista manual, evitando duplicados exactos."""
        valid_exts = {".mp4", ".avi", ".mkv", ".mov"}
        existing = set(os.path.normcase(os.path.abspath(p)) for p in self.input_videos)

        for path in file_paths:
            abs_path = os.path.abspath(path)
            ext = os.path.splitext(abs_path)[1].lower()
            norm = os.path.normcase(abs_path)

            if ext not in valid_exts:
                continue
            if norm in existing:
                continue

            self.input_videos.append(abs_path)
            item = QListWidgetItem(os.path.basename(abs_path))
            item.setToolTip(abs_path)
            item.setData(Qt.ItemDataRole.UserRole, abs_path)
            self.video_list_widget.addItem(item)
            existing.add(norm)

        self.update_videos_label()

    def remove_selected_videos(self):
        """Elimina los vídeos seleccionados de la lista manual."""
        selected_items = self.video_list_widget.selectedItems()
        if not selected_items:
            return

        selected_paths = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in selected_items
        }

        for item in selected_items:
            row = self.video_list_widget.row(item)
            self.video_list_widget.takeItem(row)

        self.input_videos = [p for p in self.input_videos if p not in selected_paths]
        self.update_videos_label()

    def clear_videos(self):
        """Limpia completamente la lista manual de vídeos."""
        self.input_videos = []
        self.video_list_widget.clear()
        self.update_videos_label()

    def move_selected_up(self):
        """Mueve hacia arriba el bloque de elementos seleccionados."""
        selected_rows = sorted(set(self.video_list_widget.row(item) for item in self.video_list_widget.selectedItems()))
        if not selected_rows or selected_rows[0] == 0:
            return

        for row in selected_rows:
            item = self.video_list_widget.takeItem(row)
            self.video_list_widget.insertItem(row - 1, item)
            item.setSelected(True)

        self.sync_input_videos_from_widget()

    def move_selected_down(self):
        """Mueve hacia abajo el bloque de elementos seleccionados."""
        selected_rows = sorted(
            set(self.video_list_widget.row(item) for item in self.video_list_widget.selectedItems()),
            reverse=True
        )
        if not selected_rows or selected_rows[0] == self.video_list_widget.count() - 1:
            return

        for row in selected_rows:
            item = self.video_list_widget.takeItem(row)
            self.video_list_widget.insertItem(row + 1, item)
            item.setSelected(True)

        self.sync_input_videos_from_widget()

    def sync_input_videos_from_widget(self, *args):
        """Sincroniza self.input_videos con el orden actual del QListWidget."""
        ordered_paths = []
        for i in range(self.video_list_widget.count()):
            item = self.video_list_widget.item(i)
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                ordered_paths.append(path)
        self.input_videos = ordered_paths
        self.update_videos_label()

    def update_videos_label(self):
        """Actualiza el contador de vídeos manuales seleccionados."""
        self.videos_label.setText(f"Videos seleccionados: {len(self.input_videos)}")

    # =========================================================
    # Carpetas
    # =========================================================
    def select_folder_1(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar Carpeta 1")
        if folder:
            self.folder_1_path = folder
            self.update_folder_label(self.folder_1_label, "Carpeta 1 (Video Campaña)", folder)

    def select_folder_2(self):
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar Carpeta 2")
        if folder:
            self.folder_2_path = folder
            self.update_folder_label(self.folder_2_label, "Carpeta 2 (Lookbook Campaña)", folder)

    def update_folder_label(self, label_widget, prefix, folder_path):
        """
        Actualiza un label de carpeta mostrando nombre visible y ruta completa en tooltip.
        """
        folder_name = os.path.basename(folder_path.rstrip("/\\"))
        label_widget.setText(f"{prefix}: <span style='color:blue;'>{folder_name}</span>")
        label_widget.setToolTip(folder_path)

    # =========================================================
    # Drag & drop
    # =========================================================
    def dragEnterEvent(self, event):
        """
        Si contiene URLs, se acepta la acción.
        """
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """
        Añade a la lista manual los archivos de vídeo válidos arrastrados.
        """
        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if os.path.isfile(file_path):
                    paths.append(file_path)
            self.add_videos(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    # =========================================================
    # Configuración visual
    # =========================================================
    def update_mode_visibility(self, mode_text):
        """Muestra u oculta parámetros según el modo seleccionado."""
        compatible_mode = (mode_text == "Compatible (recodificar)")
        self.preset_label.setVisible(compatible_mode)
        self.preset_combo.setVisible(compatible_mode)
        self.crf_label.setVisible(compatible_mode)
        self.crf_input.setVisible(compatible_mode)

    def validate_mode_inputs(self):
        """
        Valida inputs comunes del modo.
        """
        mode_text = self.mode_combo.currentText()
        if mode_text == "Compatible (recodificar)":
            crf = self.crf_input.text().strip()
            if not crf.isdigit():
                return False, "El CRF debe ser un número entero."
        return True, ""

    # =========================================================
    # Procesado manual
    # =========================================================
    def merge_videos(self):
        """
        Inicia el proceso manual de unión de vídeos.
        """
        if len(self.input_videos) < 2:
            error_widget = ConversionTaskWidget("Error: Vídeos insuficientes")
            error_widget.update_status("Selecciona al menos 2 vídeos.")
            self.tasks_layout.addWidget(error_widget)
            return

        is_valid, message = self.validate_mode_inputs()
        if not is_valid:
            error_widget = ConversionTaskWidget("Error: Configuración inválida")
            error_widget.update_status(message)
            self.tasks_layout.addWidget(error_widget)
            return

        mode_text = self.mode_combo.currentText()
        mode = "fast" if mode_text == "Rápido (sin recodificar)" else "compatible"

        try:
            command, output_file, concat_file, error_message, details = merge_videos_command(
                self.input_videos,
                mode=mode,
                output_name=self.output_name_input.text().strip(),
                preset=self.preset_combo.currentText(),
                crf=self.crf_input.text().strip(),
                output_format="mp4"
            )

            if not command:
                error_widget = ConversionTaskWidget("Error: Preparación de unión")
                error_widget.update_status(error_message or "No se pudo construir el comando FFmpeg.")
                self.tasks_layout.addWidget(error_widget)
                return

        except Exception as e:
            error_widget = ConversionTaskWidget("Error: Preparación de unión")
            error_widget.update_status(str(e))
            self.tasks_layout.addWidget(error_widget)
            return

        task_prefix = "Unión rápida: " if details["mode"] == "fast" else "Unión compatible: "
        self.start_merge_task(command, output_file, concat_file, task_prefix, details)

    # =========================================================
    # Procesado automático por carpetas
    # =========================================================
    def merge_folders_by_resolution(self):
        """
        Empareja automáticamente dos carpetas por resolución y variante,
        y lanza una fusión por cada pareja encontrada.
        """
        if not self.folder_1_path or not os.path.isdir(self.folder_1_path):
            error_widget = ConversionTaskWidget("Error: Carpeta 1")
            error_widget.update_status("Selecciona una Carpeta 1 válida.")
            self.tasks_layout.addWidget(error_widget)
            return

        if not self.folder_2_path or not os.path.isdir(self.folder_2_path):
            error_widget = ConversionTaskWidget("Error: Carpeta 2")
            error_widget.update_status("Selecciona una Carpeta 2 válida.")
            self.tasks_layout.addWidget(error_widget)
            return

        is_valid, message = self.validate_mode_inputs()
        if not is_valid:
            error_widget = ConversionTaskWidget("Error: Configuración inválida")
            error_widget.update_status(message)
            self.tasks_layout.addWidget(error_widget)
            return

        mode_text = self.mode_combo.currentText()
        mode = "fast" if mode_text == "Rápido (sin recodificar)" else "compatible"

        try:
            pairs, ignored_1, ignored_2, warnings = pair_videos_by_resolution(
                self.folder_1_path,
                self.folder_2_path
            )
        except Exception as e:
            error_widget = ConversionTaskWidget("Error: Emparejado automático")
            error_widget.update_status(str(e))
            self.tasks_layout.addWidget(error_widget)
            return

        if not pairs:
            self.auto_summary_label.setText(
                f"No se encontraron parejas compatibles. Ignorados carpeta 1: {len(ignored_1)} | "
                f"Ignorados carpeta 2: {len(ignored_2)}"
            )
            error_widget = ConversionTaskWidget("Error: Sin coincidencias")
            error_widget.update_status("No se encontraron vídeos con resolución coincidente entre ambas carpetas.")
            self.tasks_layout.addWidget(error_widget)
            return

        output_dir = os.path.join(self.folder_1_path, "merged_by_resolution")
        os.makedirs(output_dir, exist_ok=True)

        summary_parts = [
            f"Emparejados: {len(pairs)}",
            f"Ignorados carpeta 1: {len(ignored_1)}",
            f"Ignorados carpeta 2: {len(ignored_2)}"
        ]
        if warnings:
            summary_parts.append(f"Advertencias: {len(warnings)}")
        self.auto_summary_label.setText(" | ".join(summary_parts))

        for pair_info in pairs:
            output_name = os.path.splitext(os.path.basename(pair_info["video_1"]))[0]

            command, output_file, concat_file, error_message, details = merge_videos_command(
                [pair_info["video_1"], pair_info["video_2"]],
                mode=mode,
                output_name=output_name,
                preset=self.preset_combo.currentText(),
                crf=self.crf_input.text().strip(),
                output_format="mp4",
                output_dir=output_dir
            )

            if not command:
                error_widget = ConversionTaskWidget(f"Error: {output_name}")
                error_widget.update_status(error_message or "No se pudo construir el comando FFmpeg.")
                self.tasks_layout.addWidget(error_widget)
                continue

            variant_suffix = " sin logo" if pair_info["variant"] == "sin_logo" else ""
            task_prefix = f"Auto {pair_info['resolution']}{variant_suffix}: "
            self.start_merge_task(command, output_file, concat_file, task_prefix, details)

    # =========================================================
    # Arranque común de tareas
    # =========================================================
    def start_merge_task(self, command, output_file, concat_file, task_prefix, details):
        """
        Crea el widget de tarea y lanza un FFmpegWorker.
        """
        task_name = task_prefix + os.path.basename(output_file)
        task_widget = ConversionTaskWidget(task_name)
        self.tasks_layout.addWidget(task_widget)

        if details.get("notice"):
            # El modo rápido no era viable: se avisa y se explica el motivo en el tooltip
            task_widget.update_status("En progreso (recodificando)")
            task_widget.status_label.setToolTip(details["notice"])

        total_frames = details.get("total_frames") or 100
        worker = FFmpegWorker(command, total_frames=total_frames, output_file=output_file, enable_logs=False)
        self.active_workers.append(worker)

        worker.progressChanged.connect(lambda value: task_widget.update_progress(value))
        worker.finishedSignal.connect(
            lambda success, message: self.handle_merge_task_finished(
                task_widget, success, message, concat_file, worker, task_prefix, details.get("notice", "")
            )
        )
        task_widget.cancelRequested.connect(
            lambda: self.cancel_merge_task(worker, task_widget, concat_file)
        )

        worker.start()

    def handle_merge_task_finished(self, task_widget, success, message, concat_file, worker, task_prefix, notice=""):
        """
        Actualiza el widget de tarea al finalizar y limpia archivo temporal.
        """
        self.safe_remove_file(concat_file)
        self.remove_worker_reference(worker)

        if success:
            task_widget.update_status("Completado (recodificado)" if notice else "Completado")
            task_widget.update_progress(100)

            if message and os.path.exists(message):
                normalized_path = os.path.abspath(message).replace("\\", "/")
                full_name = os.path.basename(message)
                full_text = task_prefix + full_name

                metrics = QFontMetrics(task_widget.name_label.font())
                elided = metrics.elidedText(full_text, Qt.TextElideMode.ElideMiddle, 200)
                link_html = f"<a style='color:blue; text-decoration:underline;' href='#'>{elided}</a>"

                task_widget.name_label.setText(link_html)
                task_widget.name_label.setToolTip(full_text)
                task_widget.name_label.linkActivated.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(normalized_path))
                )

        elif str(message).lower() == "cancelado":
            task_widget.update_status("Cancelado")
            task_widget.update_progress(0)
        else:
            task_widget.update_status(f"Error: {message}")
            task_widget.update_progress(0)

    def cancel_merge_task(self, worker, task_widget, concat_file):
        """
        Cancela la tarea de unión.
        """
        worker.cancel()
        self.safe_remove_file(concat_file)
        task_widget.update_status("Cancelado")
        task_widget.update_progress(0)
        self.remove_worker_reference(worker)

    # =========================================================
    # Utilidades
    # =========================================================
    def safe_remove_file(self, file_path):
        """Elimina un archivo si existe, ignorando errores."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

    def remove_worker_reference(self, worker):
        """Elimina la referencia al worker cuando finaliza."""
        try:
            if worker in self.active_workers:
                self.active_workers.remove(worker)
        except Exception:
            pass