import sys
import os
import aiohttp
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import edge_tts
import json
from playsound import playsound
import logging
from openai import OpenAI
import queue
import re
import requests
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QTextEdit, QVBoxLayout, QPushButton, QLineEdit,
    QFileDialog, QComboBox, QMenuBar, QMainWindow, QMessageBox, QInputDialog
)
from PyQt6.QtGui import QTextCursor, QTextBlockFormat, QFont, QBrush, QColor, QTextCharFormat, QAction 
from PyQt6.QtCore import Qt, QEvent, QObject, pyqtSignal, QThread

# 初始化日志记录器
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()

class MultiAI(QMainWindow):
    """
    MultiAI 是一个多模型 AI 助手的图形界面应用程序。
    """
    play_audio_signal = pyqtSignal()  # 新增信号
    playback_error_signal = pyqtSignal(str)  # 新增错误信号
    def __init__(self):
        """
        初始化 MultiAI 应用程序，包括文本到语音引擎、API 客户端、模型配置以及 GUI 界面。
        """
        super().__init__()
        self.play_audio_signal.connect(self.play_audio)  # 连接信号到槽（新增）
        self.playback_error_signal.connect(self.handle_playback_error)  # 连接错误信号

        self.width = 600
        self.height = 900

        # 初始化内部变量 prompt_template
        self.web_context = ""
        self.user_query = ""
        self.prompt_template = f"""
[系统指令]                            
你是一个AI助手, 当前日期为{datetime.now().strftime('%Y-%m-%d')}。
以下是来自网络的实时信息片段(可能不完整):\n
{self.web_context}

[用户问题]
{self.user_query}
"""

        try:
            # 初始化edge_tts参数
            self.voice = "zh-CN-XiaoyiNeural"  # 使用微软晓伊语音
            self.output_file = "output.mp3"     # 固定输出文件名
        except Exception as e:
            print(f"Failed to initialize TTS: {e}")

        self.clients = {
            "local_deepseek-r1:1.5b": None,  # 本地模型不依赖 API 客户端
            "api_openai": OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url="https://api.feidaapi.com/v1"),
            "api_deepseek": OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com"),
            "api_kimi": OpenAI(api_key=os.getenv("KIMI_API_KEY"), base_url="https://api.moonshot.cn/v1"),
        }
        self.models = {
            "local_deepseek-r1:1.5b": "deepseek-r1:1.5b",
            "api_openai": "gpt-4o",
            "api_deepseek": "deepseek-chat",
            "api_kimi": "moonshot-v1-8k",
        }
        self.current_model = "local_deepseek-r1:7b"
        self.all_messages = [{"role": "system", "content": "You are a helpful assistant"}]

        self.setup_gui()

        # 初始化模型参数
        self.model_params = {
            "max_tokens": 1024,
            "temperature": 0.75,
            "top_p": 0.9,
        }

    async def async_web_search(self, query: str):
        """
        异步执行网络搜索，使用 google.serper API 返回前10个搜索结果内容列表。
        """
        api_key = os.getenv("SERPER_API_KEY")
        if api_key is None:
            logger.error("SERPER_API_KEY 未设置")
            return []
        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }
        logger.info(f"Using API key: {api_key}")
        proxy_url = os.getenv("PROXY_URL")
        async with aiohttp.ClientSession() as session:
            try:
                if proxy_url:
                    async with session.post(
                        "https://google.serper.dev/search",
                        headers=headers,
                        json={"q": query, "num": 5},
                        proxy=proxy_url
                    ) as response:
                        response.raise_for_status()
                        data = await response.json()
                else:
                    async with session.post(
                        "https://google.serper.dev/search",
                        headers=headers,
                        json={"q": query, "num": 10}
                    ) as response:
                        response.raise_for_status()
                        data = await response.json()
                return data.get("organic", [])[:10]
            except aiohttp.ClientError as e:
                logger.error(f"ClientError: {e}")
            except ValueError as e:
                logger.error(f"JSON error: {e}")
            except Exception as e:
                logger.error(f"Unknown error: {e}")
        return []

    def web_search(self, query):
        """
        同步封装的网络搜索接口，调用异步方法 async_web_search 获取搜索结果。
        """
        try:
            results = asyncio.run(self.async_web_search(query))
            if results:
                formatted_results = "\n\n".join(
                    f"Title: {result.get('title', 'N/A')}\nLink: {result.get('link', 'N/A')}\nSnippet: {result.get('snippet', 'N/A')}"
                    for result in results
                )
                return formatted_results
            else:
                return "未找到搜索结果"
        except Exception as e:
            return f"搜索异常: {e}"

    def generate_response(self, query, model):
        """
        使用本地模型根据给定的查询和模型生成回复。
        """
        # 1. 网络检索——只有当“联网搜索”按钮处于按下状态时才进行联网搜索
        if hasattr(self, 'search_button') and self.search_button.isChecked():
            self.web_context += self.web_search(query)
        
        # 2. 更新提示词
        self.prompt_template = f"""
[系统指令]                            
你是一个AI助手, 当前日期为{datetime.now().strftime('%Y-%m-%d')}。
以下是来自网络的实时信息片段(可能不完整):\n
{self.web_context}

[用户问题]
{query}
"""
        print(self.prompt_template)
        prompt = self.prompt_template
        
        # 3. 调用本地模型
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "max_tokens": self.model_params['max_tokens'],
                "temperature": self.model_params['temperature'],
                "top_p": self.model_params['top_p'],
                "history": self.all_messages
            }
        )
        return response.json()["response"]

    def setup_gui(self):
        """
        配置图形用户界面，包括窗口标题、尺寸、部件创建及布局等。
        """
        self.setWindowTitle("MultiAI Assistant")
        # 获取主屏幕的可用区域
        screen = QApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()

        self.height = int(screen_geometry.height()*0.9)
        self.width = int(screen_geometry.width()/2)
        # 计算 x 坐标，使窗口的右边缘与屏幕右边缘对齐
        x = screen_geometry.x() + screen_geometry.width() - self.width
        # 这里设置 y 坐标为屏幕高度居中，也可以根据需求调整
        y = screen_geometry.y() + (screen_geometry.height() - self.height) // 2
    
        self.setGeometry(x, y, self.width, self.height)

        self.height = int(screen_geometry.height() * 0.96)
        self.width = int(screen_geometry.width() / 2)
        x = screen_geometry.x() + screen_geometry.width() - self.width
        y = screen_geometry.y() + (screen_geometry.height() - self.height) // 2 + 20

        self.setGeometry(x, y, self.width, self.height)
        self.font = QFont("Arial", 11)

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)

        self.model_selector = QComboBox(self)
        self.model_selector.addItems(self.models.keys())
        self.model_selector.currentTextChanged.connect(self.select_model)
        layout.addWidget(self.model_selector)

        self.output_area = QTextEdit(self)
        self.output_area.setReadOnly(False)
        self.output_area.setStyleSheet("background-color: #F0F0F0;")
        self.output_area.setFont(self.font)
        layout.addWidget(self.output_area)

        self.send_button = QPushButton("发送", self)
        self.send_button.clicked.connect(self.send_message)
        layout.addWidget(self.send_button)
        self.send_button.hide()  # 隐藏发送按钮

        # 创建 QTextEdit 作为用户输入区域
        self.entry = QTextEdit(self)
        self.entry.setMinimumHeight(50)
        self.entry.setMaximumHeight(100)
        self.entry.setReadOnly(False)
        self.entry.setPlaceholderText("主人，你好！请说出你的问题，回车键发送。")
        self.entry.installEventFilter(self)
        layout.addWidget(self.entry)

        # 在 self.entry 内部左下角增加“联网搜索”按钮
        self.search_button = QPushButton("联网", self.entry)
        self.search_button.setCheckable(True)
        self.search_button.setFixedSize(50, 20)
        # 初始位置：距离左边5像素，距离底部5像素
        self.search_button.move(5, self.entry.height() - self.search_button.height() - 5)

        # 再增加一个“推理”按钮，放在“联网搜索”按钮的右侧
        self.think_button = QPushButton("推理", self.entry)
        self.think_button.setCheckable(True)
        self.think_button.setFixedSize(50, 20)
        # 初始位置：紧挨“联网搜索”按钮，间隔5像素
        self.think_button.move(self.search_button.x() + self.search_button.width() + 5,
                               self.entry.height() - self.think_button.height() - 5)

        # 再增加一个"录音"按钮，放在"推理"按钮的右侧
        self.record_button = QPushButton("录音", self.entry)
        self.record_button.setCheckable(True)
        self.record_button.setFixedSize(50, 20)
        # 初始位置：紧挨"推理"按钮，间隔5像素
        self.record_button.move(self.think_button.x() + self.think_button.width() + 5,
                                self.entry.height() - self.record_button.height() - 5)

        self.setCentralWidget(central_widget)

        # 添加菜单栏
        menubar = self.menuBar()
        file_menu = menubar.addMenu("提示词")
        dialog_menu = menubar.addMenu("对话")
        search_menu = menubar.addMenu("搜索结果")
        model_menu = menubar.addMenu('模型参数')

        load_prompt_action = QAction("加载提示词", self)
        load_prompt_action.triggered.connect(self.load_prompt_file)
        file_menu.addAction(load_prompt_action)

        save_prompt_action = QAction("保存提示词", self)
        save_prompt_action.triggered.connect(self.save_prompt_file)
        file_menu.addAction(save_prompt_action)

        show_prompt_action = QAction("显示提示词", self)
        show_prompt_action.triggered.connect(self.show_prompt)
        file_menu.addAction(show_prompt_action)

        save_message_action = QAction("保存对话", self)
        save_message_action.triggered.connect(self.save_message)
        dialog_menu.addAction(save_message_action)

        load_message_action = QAction("加载对话", self)
        load_message_action.triggered.connect(self.load_message)
        dialog_menu.addAction(load_message_action)

        clear_message_action = QAction("清除对话", self)
        clear_message_action.triggered.connect(self.clear_message)
        dialog_menu.addAction(clear_message_action)

        show_message_action = QAction("显示对话", self)
        show_message_action.triggered.connect(self.show_message)
        dialog_menu.addAction(show_message_action)

        show_web_context_action = QAction("显示搜索结果", self)
        show_web_context_action.triggered.connect(self.show_web_context)
        search_menu.addAction(show_web_context_action)

        clear_web_context_action = QAction("清空搜索结果", self)
        clear_web_context_action.triggered.connect(self.clear_web_context)
        search_menu.addAction(clear_web_context_action)

        view_model_action = QAction('查看模型参数', self)
        view_model_action.triggered.connect(self.show_model)
        model_menu.addAction(view_model_action)

        edit_model_action = QAction('编辑模型参数', self)
        edit_model_action.triggered.connect(self.edit_model)
        model_menu.addAction(edit_model_action)

    def eventFilter(self, obj, event):
        # 对 self.entry 的事件处理
        if obj is self.entry:
            if event.type() == QEvent.Type.Resize:
                # 同时调整“联网搜索”和“推理”按钮的位置，确保始终位于左下角
                self.search_button.move(5, self.entry.height() - self.search_button.height() - 5)
                self.think_button.move(self.search_button.x() + self.search_button.width() + 5,
                                       self.entry.height() - self.think_button.height() - 5)
                self.record_button.move(self.think_button.x() + self.think_button.width() + 5,
                                       self.entry.height() - self.record_button.height() - 5)
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                        self.entry.insertPlainText('\n')
                    else:
                        self.send_message()
                    return True
        return super().eventFilter(obj, event)

    def show_model(self):
        # 显示当前模型的参数
        param_str = "\n".join([f"{key}: {value}" for key, value in self.model_params.items()])
        QMessageBox.information(self, "模型参数", f"当前模型参数:\n{param_str}")

    def edit_model(self):
        """编辑模型参数，每次创建新的对话框实例"""
        # 编辑max_tokens
        max_tokens, ok = QInputDialog.getText(self, \
            "编辑模型", \
            "max_tokens (例如: 512, 1024):", \
            QLineEdit.EchoMode.Normal, \
            str(self.model_params['max_tokens']))
        if ok:
            try:
                self.model_params['max_tokens'] = int(max_tokens)
            except ValueError:
                QMessageBox.warning(self, "输入无效", "请输入有效的整数")
                return  # 输入错误时提前返回
    
        # 编辑temperature
        temperature, ok = QInputDialog.getText(self,\
            "编辑模型",\
            "输入temperature (0至1, 例如: 0.8):",\
            QLineEdit.EchoMode.Normal,\
            str(self.model_params['temperature']))
        if ok:
            try:
                temp = float(temperature)
                if not 0 <= temp <= 1:
                    raise ValueError
                self.model_params['temperature'] = temp
            except ValueError:
                QMessageBox.warning(self, "输入无效", "请输入0到1之间的有效小数")
                return
    
        # 编辑top_p
        top_p, ok = QInputDialog.getText(self,\
            "编辑模型",\
            "top_p (0至1, 例如: 0.9):",\
            QLineEdit.EchoMode.Normal,\
            str(self.model_params['top_p']))
        if ok:
            try:
                p = float(top_p)
                if not 0 <= p <= 1:
                    raise ValueError
                self.model_params['top_p'] = p
            except ValueError:
                QMessageBox.warning(self, "输入无效", "请输入0到1之间的有效小数")
                return
    
        QMessageBox.information(self, "更新成功", "模型参数已更新")

    def _insert_message_block(self, message, bg_color, text_color, prefix=None):
        """
        将消息块以Markdown风格格式化显示，提升排版美观度。
        采用HTML标签模拟Markdown效果，包含标题（prefix）和预格式化文本（message）。
        """
        cursor = self.output_area.textCursor()
        block_format = QTextBlockFormat()
        block_format.setAlignment(Qt.AlignmentFlag.AlignLeft)

        # 设置 block_format 的背景色
        block_format.setBackground(QBrush(bg_color))

        # 创建一个字符格式并设置字体颜色
        char_format = QTextCharFormat()
        char_format.setForeground(QBrush(QColor(text_color)))
        
        # 获取背景颜色的十六进制字符串（例如：#F0F0F0）
        bg_hex = bg_color.name() if hasattr(bg_color, "name") else bg_color
    
        # 构造HTML内容，整体使用一个div容器，并设置内边距、圆角和外边距
        # 如果提供prefix，则以加粗的<p>标签显示；消息内容放在<pre>标签内，保留空格与换行效果
        html_content = f'''
        <div style="background-color: {bg_hex}; padding: 10px; border-radius: 8px; margin: 8px 0;">
        {"<p style='margin:0; font-weight:bold; color:" + text_color + ";'>" + prefix.strip() + "</p>" if prefix else ""}
        <pre style="margin:5px 0; background-color: {bg_hex}; color: {text_color}; white-space: pre-wrap;">{message}
        </pre></div>'''
        
        # 插入一个新的文本块并设置HTML内容
        cursor.insertBlock(block_format, char_format)
        cursor.insertHtml(html_content)
        self.output_area.setTextCursor(cursor)

    def _load_json_file(self, dialog_title, success_message, error_prefix):
        file_path, _ = QFileDialog.getOpenFileName(self, dialog_title, "", "JSON Files (*.json)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    if isinstance(data, list):
                        self.all_messages = data
                        self.output_area_sys_message(success_message)
                    else:
                        self.output_area_sys_message("文件内容格式错误！")
            except Exception as e:
                self.output_area_sys_message(f"{error_prefix}{e}")

    def send_message(self):
        """
        处理用户消息发送，显示用户消息，并调用 AI 获取回复，然后显示回复。
        """
        user_message = self.entry.toPlainText().strip()
        if user_message:
            self._insert_message_block(user_message, QColor(000, 000, 255), "white", prefix="user: ")
            self.entry.clear()
            # 异步调用 ask_question
            self.ask_question_async(user_message)

    def ask_question_async(self, question):
        """
        异步调用 ask_question 方法
        """
        self.worker = Worker(self.ask_question, question)
        self.worker.result_signal.connect(self.handle_answer)
        self.worker.start()

    def handle_answer(self, ai_response):
        # 拆分回复中的 <think> 部分和其他部分
        parts = re.split(r'(<think>.*?</think>)', ai_response, flags=re.IGNORECASE | re.DOTALL)
        think_content = ""
        for part in parts:
            if part.startswith('<think>') and part.endswith('</think>'):
                think_content = part[7:-8]  # 去掉 <think> 标签
            elif part:
                ai_response = part

        # 仅当“推理”按钮处于按下状态且 think_content 存在时显示推理内容
        if think_content and hasattr(self, 'think_button') and self.think_button.isChecked():
            self._insert_message_block(think_content, QColor(240, 240, 240), "grey",
                                       prefix=self.current_model + " THINK\n")

        if ai_response:
            self._insert_message_block(ai_response, QColor(144, 238, 144), "black", prefix=self.current_model + " REPLY\n")
            self.text_to_speech(ai_response)
            self._insert_message_block(ai_response, QColor(250, 240, 000), "black",
                                       prefix=self.current_model + " REPLY\n")
            # 仅当录音按钮被按下时生成语音
            if hasattr(self, 'record_button') and self.record_button.isChecked():
                self.text_to_speech(ai_response)

    def ask_question(self, question):
        """
        向当前选定的 AI 模型提问，并返回模型回复。
        """
        self.all_messages.append({"role": "user", "content": question})
        if "local" in self.current_model:
            model = self.models[self.current_model]
            answer = self.generate_response(question, model)
            self.all_messages.append({"role": "assistant", "content": answer})
            return answer
        else:
            # 仅当“联网搜索”按钮处于按下状态时进行联网检索
            if hasattr(self, 'search_button') and self.search_button.isChecked():
                self.web_context += self.web_search(question)
            self.prompt_template = f"""
[系统指令]                            
你是一个AI助手, 当前日期为{datetime.now().strftime('%Y-%m-%d')}。
以下是来自网络的实时信息片段(可能不完整):\n
{self.web_context}

[用户问题]
{question}
"""
            print(self.prompt_template)

            self.all_messages.append({"role": "user", "content": self.prompt_template})
            client = self.clients[self.current_model]
            model = self.models[self.current_model]
            try:
                response = client.chat.completions.create(
                    model = model,
                    messages = self.all_messages,
                    max_tokens = self.model_params['max_tokens'],
                    temperature = self.model_params['temperature'],
                    top_p = self.model_params['top_p'],
                    stream=False,
                )
                answer = response.choices[0].message.content
                self.all_messages.append({"role": "assistant", "content": answer})
                return answer
            except Exception as e:
                return f"API请求失败: {str(e)}"

    def select_model(self, model):
        """
        根据用户选择更新当前使用的模型。
        """
        self.current_model = model
        self.output_area_sys_message(f"已切换到 {model} 模型\n")

    def text_to_speech(self, text):
        """
        使用edge_tts生成语音并保存为MP3文件
        """
        async def async_tts():
            try:
                communicate = edge_tts.Communicate(text, self.voice)
                await communicate.save(self.output_file)
                logger.info(f"语音已保存为 {self.output_file}")

                  # 播放音频文件（在GUI主线程中执行）
                self.play_audio_signal.emit()  # 使用信号触发播放      except Exception as e:
            except Exception as e:
                logger.error(f"语音生成失败: {str(e)}")

        # 在Qt事件循环中运行异步任务
        try:
            asyncio.run(async_tts())
        except RuntimeError:
            print("Event loop is already running.")

    def handle_playback_error(self, error_msg):
        """处理播放错误"""
        QMessageBox.warning(self, "播放错误", f"无法播放语音：{error_msg}")

    def play_audio(self):
        """使用线程播放音频的槽函数"""
        def run():
            try:
                playsound(self.output_file)
            except Exception as e:
                logger.error(f"播放失败: {str(e)}")
                self.playback_error_signal.emit(str(e))
        
        import threading
        thread = threading.Thread(target=run)
        thread.start()

    def load_prompt_file(self):
        self._load_json_file("加载提示文件", "成功加载提示文件！", "加载文件失败: ")

    def save_prompt_file(self):
        template_dict = {"prompt_template": self.prompt_template}
        template_list = [template_dict]

        file_path, _ = QFileDialog.getSaveFileName(self, "保存对话", "", "JSON Files (*.json)")
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as file:
                    json.dump(template_list, file, ensure_ascii=False, indent=4)
                    self.output_area_sys_message("提示词语已成功保存！")
            except Exception as e:
                self.output_area_sys_message(f"保存提示词失败: {e}")

    def load_message(self):
        self._load_json_file("加载对话", "成功加载对话！", "加载对话失败: ")

    def output_area_sys_message(self, sys_message):
        cursor = self.output_area.textCursor()
        block_format = QTextBlockFormat()
        block_format.setAlignment(Qt.AlignmentFlag.AlignLeft)
        block_format.setBackground(QBrush(QColor(255, 255, 255)))

        cursor.insertBlock(block_format)
        cursor.insertText("\n" + sys_message)
        self.output_area.setTextCursor(cursor)
        self.output_area.ensureCursorVisible()

    def save_message(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "保存对话", "", "JSON Files (*.json)")
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as file:
                    json.dump(self.all_messages, file, ensure_ascii=False, indent=4)
                    self.output_area_sys_message("对话已成功保存！")
            except Exception as e:
                self.output_area_sys_message(f"保存对话失败: {e}")

    def clear_message(self):
        self.all_messages = [{"role": "system", "content": "You are a helpful assistant"}]
        self.output_area_sys_message("对话已清除！")
        self.output_area_sys_message(f"{json.dumps(self.all_messages, indent=4, ensure_ascii=False)}\n")

    def show_message(self):
        self.output_area_sys_message("显示对话")
        formatted_messages = json.dumps(self.all_messages, indent=4, ensure_ascii=False)
        self.output_area_sys_message(formatted_messages)

    def show_web_context(self):
        self.output_area_sys_message("以下是搜索结果")
        self.output_area_sys_message(self.web_context)

    def clear_web_context(self):
        self.web_context = ""

    def show_prompt(self):
        self.output_area_sys_message("显示提示词")
        self.output_area_sys_message(self.prompt_template)

class Worker(QThread):
    result_signal = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        result = self.func(*self.args, **self.kwargs)
        self.result_signal.emit(result)

if __name__ == "__main__":
    app = QApplication(sys.argv)

    # 创建并设置事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    window = MultiAI()
    window.show()

    # 启动Qt事件循环
    sys.exit(app.exec())
