import os
import datetime
import time
import threading
import hashlib
import logging
from flask import Flask, render_template, send_from_directory, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
import io
import base64
import shutil
import uuid
import json

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_for_testing_only')

# 允许大量高分辨率图片上传
app.config['MAX_CONTENT_LENGTH'] = 2000 * 1024 * 1024  # 2GB总请求大小
app.config['MAX_SINGLE_FILE_SIZE'] = 500 * 1024 * 1024  # 单个文件500MB

# 增加大文件上传超时时间
app.config['TIMEOUT'] = 3600  # 1小时超时

# 配置分块上传参数
app.config['CHUNK_SIZE'] = 4 * 1024 * 1024  # 4MB分块大小

# 初始化Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# 用户模型
class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

# 创建管理员用户
admin_user = User(1, 'admin', generate_password_hash('Admin1234'))

# 用户加载函数
@login_manager.user_loader
def load_user(user_id):
    if int(user_id) == 1:
        return admin_user
    return None

# 配置文件夹路径
PHOTOS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'photos')
CACHE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
THUMBNAIL_FOLDER = os.path.join(CACHE_FOLDER, 'thumbnails')
THUMBNAIL_MAX_SIZE = 800  # 缩略图最大尺寸（宽或高）
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

# 确保必要的文件夹存在
if not os.path.exists(PHOTOS_FOLDER):
    os.makedirs(PHOTOS_FOLDER)
if not os.path.exists(CACHE_FOLDER):
    os.makedirs(CACHE_FOLDER)
if not os.path.exists(THUMBNAIL_FOLDER):
    os.makedirs(THUMBNAIL_FOLDER)
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# 图片缓存锁，防止并发操作导致的问题
cache_lock = threading.Lock()

# 允许的图片扩展名
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif'}

# 退出标志，用于优雅退出缓存监控线程
exit_flag = False

# 全局标志，控制是否处于快速启动模式
FAST_START = True  # 默认使用快速启动模式

# 状态信息
cache_status = {
    "processing": False,
    "total": 0,
    "processed": 0,
    "percent": 0,
    "last_update": None
}

# 生成图片缓存的文件名
def get_cache_filename(original_filename, width, height):
    # 使用原始文件名和目标尺寸创建一个唯一的缓存文件名
    name, ext = os.path.splitext(original_filename)
    return f"{name}_{width}x{height}{ext}"

# 检查缓存是否存在且最新
def is_cache_valid(original_path, cache_path):
    # 如果缓存不存在，返回False
    if not os.path.exists(cache_path):
        return False
    
    # 检查原始文件是否比缓存更新
    original_mtime = os.path.getmtime(original_path)
    cache_mtime = os.path.getmtime(cache_path)
    
    # 如果原始文件更新，则缓存无效
    return original_mtime <= cache_mtime

# 生成并保存缩略图到缓存
def generate_thumbnail(file_path, cache_path, max_size=THUMBNAIL_MAX_SIZE):
    try:
        # 规范化路径
        file_path = os.path.normpath(file_path)
        cache_path = os.path.normpath(cache_path)
        
        logging.info(f"生成缩略图 - 原始文件: {file_path}")
        logging.info(f"生成缩略图 - 缓存路径: {cache_path}")
        
        # 确保原始文件存在
        if not os.path.exists(file_path):
            logging.error(f"原始文件不存在，无法生成缩略图: {file_path}")
            return None, 0, 0
        
        # 确保缓存目录存在
        cache_dir = os.path.dirname(cache_path)
        if not os.path.exists(cache_dir):
            try:
                os.makedirs(cache_dir, exist_ok=True)
                logging.info(f"创建缓存目录: {cache_dir}")
            except Exception as e:
                logging.error(f"创建缓存目录失败: {e}")
                return None, 0, 0
        
        # 记录文件大小
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logging.info(f"原始文件大小: {file_size_mb:.1f} MB")
        
        with Image.open(file_path) as img:
            # 获取原始图片尺寸
            orig_width, orig_height = img.size
            logging.info(f"原始图片尺寸: {orig_width}x{orig_height}")
            
            # 计算缩放比例，保持原始宽高比
            ratio = min(max_size / orig_width, max_size / orig_height)
            new_width = int(orig_width * ratio)
            new_height = int(orig_height * ratio)
            logging.info(f"缩略图目标尺寸: {new_width}x{new_height}")
            
            # 调整图片大小
            thumbnail = img.resize((new_width, new_height), Image.LANCZOS)
            
            # 保存到缓存文件夹
            img_format = img.format
            try:
                thumbnail.save(cache_path, format=img_format)
                logging.info(f"成功保存缩略图: {cache_path}")
                
                # 确认缩略图是否实际创建
                if not os.path.exists(cache_path):
                    logging.error(f"缩略图保存失败，文件不存在: {cache_path}")
                    return None, 0, 0
                
                thumbnail_size_mb = os.path.getsize(cache_path) / (1024 * 1024)
                logging.info(f"缩略图大小: {thumbnail_size_mb:.1f} MB")
            except Exception as save_error:
                logging.error(f"保存缩略图时出错: {save_error}")
                
                # 尝试使用不同的保存方式
                try:
                    # 保存到临时路径然后移动
                    temp_cache_path = cache_path + ".temp"
                    thumbnail.save(temp_cache_path, format=img_format)
                    logging.info(f"保存到临时路径: {temp_cache_path}")
                    
                    if os.path.exists(temp_cache_path):
                        if os.path.exists(cache_path):
                            os.remove(cache_path)
                        shutil.move(temp_cache_path, cache_path)
                        logging.info(f"移动临时缩略图到最终位置: {temp_cache_path} -> {cache_path}")
                    else:
                        logging.error(f"临时缩略图文件不存在: {temp_cache_path}")
                        return None, 0, 0
                except Exception as alt_save_error:
                    logging.error(f"替代保存方式也失败: {alt_save_error}")
                    return None, 0, 0
            
            logging.info(f"已为 {os.path.basename(file_path)} 生成缩略图缓存")
            return thumbnail, new_width, new_height
    except Exception as e:
        logging.error(f"生成缩略图时出错: {e}")
        return None, 0, 0

# 压缩图片
def optimize_image(input_path, output_path=None, quality=92, target_size_mb=5, preserve_size=False):
    """
    优化图片大小但不损失画质 - 单次压缩模式
    :param input_path: 输入图片路径
    :param output_path: 输出图片路径，如果为None则覆盖原图
    :param quality: 图片质量，范围1-100，默认92（高质量）
    :param target_size_mb: 目标文件大小（MB），默认5MB
    :param preserve_size: 是否保持原始尺寸
    """
    # 规范化路径
    input_path = os.path.normpath(input_path)
    
    if output_path is None:
        output_path = input_path
    else:
        output_path = os.path.normpath(output_path)
    
    # 记录路径信息
    logging.info(f"优化图片 - 输入路径: {input_path}")
    logging.info(f"优化图片 - 输出路径: {output_path}")
    
    # 确保输入路径存在
    if not os.path.exists(input_path):
        logging.error(f"输入文件不存在: {input_path}")
        return 0
    
    # 确保目标文件夹存在
    output_dir = os.path.dirname(output_path)
    if not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
            logging.info(f"创建目标文件夹: {output_dir}")
        except Exception as e:
            logging.error(f"创建目标文件夹失败: {e}")
            return 0
        
    try:
        # 打开图片
        img = Image.open(input_path)
        
        # 获取图片格式
        img_format = img.format or 'JPEG'
        
        # 获取原始图片大小和尺寸
        original_size = os.path.getsize(input_path)
        original_size_mb = original_size / (1024 * 1024)
        original_width, original_height = img.size
        
        logging.info(f"原始图片信息: 格式={img_format}, 尺寸={original_width}x{original_height}, 大小={original_size_mb:.1f}MB")
        
        # 如果原图已经足够小且需要保持原始尺寸，直接使用原图
        if preserve_size and original_size_mb <= target_size_mb:
            if input_path != output_path:
                try:
                    shutil.copy2(input_path, output_path)
                    logging.info(f"图片已经足够小 ({original_size_mb:.1f}MB), 已复制原图")
                except Exception as copy_error:
                    logging.error(f"复制原图失败: {copy_error}")
                    # 尝试使用二进制方式复制
                    try:
                        with open(input_path, 'rb') as src_file:
                            with open(output_path, 'wb') as dst_file:
                                dst_file.write(src_file.read())
                        logging.info(f"使用二进制模式复制图片: {input_path} -> {output_path}")
                    except Exception as bin_error:
                        logging.error(f"二进制复制也失败: {bin_error}")
                        return 0
            return 0
            
        # 根据原始图片大小直接确定合适的压缩质量
        optimal_quality = 85  # 默认质量
        
        # 根据图片大小进行简单的质量选择
        if original_size_mb > 100:  # 超大图片(>100MB)
            optimal_quality = 70
        elif original_size_mb > 50:  # 大图片(50-100MB)
            optimal_quality = 75
        elif original_size_mb > 20:  # 中大图片(20-50MB)
            optimal_quality = 80
        elif original_size_mb > 10:  # 中等图片(10-20MB)
            optimal_quality = 82
        elif original_size_mb > 5:   # 小图片(5-10MB)
            optimal_quality = 85
        else:                        # 极小图片(<5MB)
            optimal_quality = 90
            
        logging.info(f"根据图片大小({original_size_mb:.1f}MB)选择压缩质量: {optimal_quality}")
        
        # 确定是否需要调整尺寸
        process_img = img
        if not preserve_size and (original_width > 4000 or original_height > 4000):
            # 对于超大图片，缩小尺寸以提高压缩效率
            max_dimension = 4000
            ratio = min(max_dimension / original_width, max_dimension / original_height)
            new_width = int(original_width * ratio)
            new_height = int(original_height * ratio)
            process_img = img.resize((new_width, new_height), Image.LANCZOS)
            logging.info(f"调整图片尺寸为: {new_width}x{new_height}")
            
        # 临时文件路径
        temp_output = output_path + ".temp"
        temp_output = os.path.normpath(temp_output)
            
        # 一次性压缩图片
        try:
            logging.info(f"开始压缩图片(质量:{optimal_quality}): {temp_output}")
            
            if img_format in ['JPEG', 'JPG']:
                # JPEG图片压缩
                process_img.save(temp_output, 
                        format=img_format,
                        quality=optimal_quality, 
                        optimize=True,
                        progressive=True,
                        subsampling=0 if optimal_quality > 80 else 2)
            elif img_format == 'PNG':
                # PNG图片压缩
                process_img.save(temp_output,
                        format=img_format,
                        optimize=True,
                        quality=optimal_quality if not preserve_size else None)
            else:
                # 其他格式
                process_img.save(temp_output,
                        format=img_format,
                        quality=optimal_quality,
                        optimize=True)
                
            logging.info(f"成功保存压缩图片: {temp_output}")
                
            # 检查压缩结果
            if os.path.exists(temp_output):
                compressed_size = os.path.getsize(temp_output)
                compressed_size_mb = compressed_size / (1024 * 1024)
                compression_ratio = (1 - compressed_size / original_size) * 100
                
                logging.info(f"压缩结果: 大小={compressed_size_mb:.1f}MB, 压缩率={compression_ratio:.1f}%")
                
                # 移动到最终位置
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                        logging.info(f"删除已存在的输出文件: {output_path}")
                    except Exception as rm_error:
                        logging.error(f"删除已存在文件失败: {rm_error}")
                
                try:
                    shutil.move(temp_output, output_path)
                    logging.info(f"移动压缩文件到最终位置: {temp_output} -> {output_path}")
                except Exception as move_error:
                    logging.error(f"移动文件失败: {move_error}")
                    # 尝试复制
                    try:
                        shutil.copy2(temp_output, output_path)
                        logging.info(f"复制压缩文件到最终位置: {temp_output} -> {output_path}")
                        os.remove(temp_output)
                    except Exception as copy_error:
                        logging.error(f"复制文件失败: {copy_error}")
                
                # 最终检查
                if os.path.exists(output_path):
                    final_size = os.path.getsize(output_path)
                    final_size_mb = final_size / (1024 * 1024)
                    final_ratio = (1 - final_size / original_size) * 100
                    
                    logging.info(f"压缩完成: {os.path.basename(input_path)}: {final_ratio:.1f}% 压缩率, " 
                                f"从 {original_size_mb:.1f}MB 压缩到 {final_size_mb:.1f}MB, 质量: {optimal_quality}")
                    return final_ratio
                else:
                    logging.error(f"最终输出文件不存在: {output_path}")
            else:
                logging.error(f"压缩文件不存在: {temp_output}")
        except Exception as save_error:
            logging.error(f"保存压缩图片失败: {save_error}")
            
        # 如果压缩失败，直接复制原图
        if input_path != output_path and os.path.exists(input_path) and not os.path.exists(output_path):
            try:
                shutil.copy2(input_path, output_path)
                logging.info(f"压缩失败，直接复制原图: {input_path} -> {output_path}")
                return 0
            except Exception as final_error:
                logging.error(f"最终复制也失败: {final_error}")
                return 0
        
        return 0
    except Exception as e:
        logging.error(f"压缩图片时出错: {e}")
        # 如果发生错误，尝试直接复制原文件
        if input_path != output_path and os.path.exists(input_path):
            try:
                # 确保目标文件夹存在
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                # 尝试复制
                shutil.copy2(input_path, output_path)
                logging.info(f"发生错误，直接复制原图: {os.path.basename(input_path)}")
                return 0
            except Exception as copy_error:
                logging.error(f"复制原图失败: {copy_error}")
                return 0
        return 0

# 清理不再需要的缓存
def clean_unused_cache():
    with cache_lock:
        try:
            # 获取所有原始图片的文件名
            original_filenames = set()
            for filename in os.listdir(PHOTOS_FOLDER):
                if os.path.isfile(os.path.join(PHOTOS_FOLDER, filename)) and \
                   os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS:
                    original_filenames.add(os.path.splitext(filename)[0])
            
            # 检查缓存文件夹中的每个文件
            deleted_count = 0
            for cache_dir in [THUMBNAIL_FOLDER]:
                if os.path.exists(cache_dir):
                    for cache_file in os.listdir(cache_dir):
                        is_valid = False
                        
                        # 遍历所有原始文件名，检查缓存文件是否匹配（前缀匹配）
                        for orig_name in original_filenames:
                            # 例如，原文件"photo123.jpg"的缓存可能是"photo123_800x800.jpg"
                            if cache_file.startswith(orig_name + "_"):
                                is_valid = True
                                break
                        
                        # 如果没找到匹配的原始文件，则删除缓存
                        if not is_valid:
                            cache_path = os.path.join(cache_dir, cache_file)
                            try:
                                os.remove(cache_path)
                                deleted_count += 1
                                logging.info(f"删除无用缓存: {cache_file}")
                            except Exception as e:
                                logging.error(f"删除缓存文件时出错: {e}")
            
            if deleted_count > 0:
                logging.info(f"共删除 {deleted_count} 个无用缓存文件")
        except Exception as e:
            logging.error(f"清理缓存时出错: {e}")

# 修改获取照片的函数，移除对预处理的引用
def get_photos():
    photos = []
    
    for filename in sorted(os.listdir(PHOTOS_FOLDER), key=lambda x: os.path.getmtime(os.path.join(PHOTOS_FOLDER, x)), reverse=True):
        file_path = os.path.join(PHOTOS_FOLDER, filename)
        ext = os.path.splitext(filename)[1].lower()
        
        if os.path.isfile(file_path) and ext in ALLOWED_EXTENSIONS:
            try:
                # 获取文件修改时间
                mod_time = os.path.getmtime(file_path)
                date_modified = datetime.datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d')
                
                # 获取文件大小
                file_size = os.path.getsize(file_path)
                if file_size < 1024 * 1024:
                    size_str = f"{file_size / 1024:.1f} KB"
                else:
                    size_str = f"{file_size / (1024 * 1024):.1f} MB"
                
                # 获取原始图片信息
                with Image.open(file_path) as img:
                    orig_width, orig_height = img.size
                    img_format = img.format
                
                # 使用缓存的缩略图
                cache_filename = get_cache_filename(filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
                cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
                
                if not os.path.exists(cache_path):
                    # 如果缓存不存在，立即生成缩略图
                    thumbnail, new_width, new_height = generate_thumbnail(file_path, cache_path)
                else:
                    # 如果缓存存在，直接使用
                    with Image.open(cache_path) as thumb:
                        new_width, new_height = thumb.size
                
                # 生成缩略图的URL路径 - 确保路径前缀与路由匹配
                thumbnail_url = f"/thumbnails/{cache_filename}"
                
                # 简化文件名显示
                display_name = filename
                if len(display_name) > 25:
                    name_parts = os.path.splitext(display_name)
                    display_name = name_parts[0][:22] + '...' + name_parts[1]
                
                photos.append({
                    'filename': filename,
                    'display_name': display_name,
                    'thumbnail': thumbnail_url,
                    'date': date_modified,
                    'size': size_str,
                    'dimensions': f"{orig_width}x{orig_height}",
                    'width': new_width,
                    'height': new_height,
                    'aspect_ratio': orig_width / orig_height
                })
            except Exception as e:
                logging.error(f"处理图片 {filename} 时出错: {e}")
    
    return photos

# 修改启动请求处理函数，移除缓存监控
@app.before_request
def startup():
    global startup_complete
    if not getattr(app, 'startup_complete', False):
        # 第一次请求时执行，替代before_first_request
        app.startup_complete = True
        logging.info("第一个请求已处理")

# 登录页面
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == admin_user.username and check_password_hash(admin_user.password_hash, password):
            login_user(admin_user)
            return redirect(url_for('admin'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

# 登出
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# 管理后台
@app.route('/admin')
@login_required
def admin():
    photos = get_photos()
    return render_template('admin.html', photos=photos)

# 分块上传相关目录
TEMP_CHUNKS_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_chunks')

# 确保临时分块目录存在
if not os.path.exists(TEMP_CHUNKS_FOLDER):
    os.makedirs(TEMP_CHUNKS_FOLDER)

# 分块上传 - 初始化上传
@app.route('/admin/upload/initialize', methods=['POST'])
@login_required
def initialize_upload():
    """Initialize chunk upload, create temporary upload ID and directory"""
    try:
        # 生成唯一上传ID
        upload_id = str(uuid.uuid4())
        
        # 创建临时目录存储分块
        upload_dir = os.path.join(TEMP_CHUNKS_FOLDER, upload_id)
        os.makedirs(upload_dir)
        
        # 获取文件信息
        file_info = request.json
        original_filename = file_info.get('filename', '')
        file_size = file_info.get('fileSize', 0)
        total_chunks = file_info.get('totalChunks', 0)
        
        # 检查文件名和扩展名
        if not original_filename:
            return jsonify({'success': False, 'message': 'Filename cannot be empty'}), 400
            
        ext = os.path.splitext(original_filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'success': False, 'message': 'Unsupported file format'}), 400
        
        # 存储上传信息
        info_file = os.path.join(upload_dir, 'info.json')
        with open(info_file, 'w') as f:
            json.dump({
                'originalFilename': original_filename,
                'fileSize': file_size,
                'totalChunks': total_chunks,
                'receivedChunks': 0,
                'timestamp': datetime.datetime.now().isoformat(),
                'status': 'initialized'
            }, f)
        
        return jsonify({
            'success': True, 
            'uploadId': upload_id,
            'message': 'Upload initialized successfully'
        })
    except Exception as e:
        logging.error(f"Error initializing upload: {e}")
        return jsonify({'success': False, 'message': f'Failed to initialize upload: {str(e)}'}), 500

# 分块上传 - 上传分块
@app.route('/admin/upload/chunk', methods=['POST'])
@login_required
def upload_chunk():
    """Receive and store a single chunk"""
    try:
        upload_id = request.form.get('uploadId')
        chunk_index = int(request.form.get('chunkIndex', 0))
        
        if not upload_id:
            return jsonify({'success': False, 'message': 'Invalid upload ID'}), 400
        
        upload_dir = os.path.join(TEMP_CHUNKS_FOLDER, upload_id)
        if not os.path.exists(upload_dir):
            return jsonify({'success': False, 'message': 'Upload session does not exist or has expired'}), 404
        
        # 检查是否有文件
        if 'chunk' not in request.files:
            return jsonify({'success': False, 'message': 'No file chunk'}), 400
        
        chunk_file = request.files['chunk']
        if chunk_file.filename == '':
            return jsonify({'success': False, 'message': 'Empty file chunk'}), 400
        
        # 保存分块
        chunk_path = os.path.join(upload_dir, f'chunk_{chunk_index:06d}')
        chunk_file.save(chunk_path)
        
        # 更新信息文件
        info_file = os.path.join(upload_dir, 'info.json')
        info = {}
        with open(info_file, 'r') as f:
            info = json.load(f)
        
        info['receivedChunks'] += 1
        # 计算上传进度百分比
        progress = int((info['receivedChunks'] / info['totalChunks']) * 100)
        info['progress'] = progress
        
        # 检查是否完成上传
        is_complete = info['receivedChunks'] >= info['totalChunks']
        if is_complete:
            info['status'] = 'complete'
        
        with open(info_file, 'w') as f:
            json.dump(info, f)
        
        return jsonify({
            'success': True, 
            'chunkIndex': chunk_index,
            'received': info['receivedChunks'],
            'total': info['totalChunks'],
            'progress': progress,
            'isComplete': is_complete,
            'message': 'Chunk uploaded successfully'
        })
    except Exception as e:
        logging.error(f"Error uploading chunk: {e}")
        return jsonify({'success': False, 'message': f'Failed to upload chunk: {str(e)}'}), 500

# 分块上传 - 完成上传
@app.route('/admin/upload/complete', methods=['POST'])
@login_required
def complete_upload():
    """Complete upload, merge chunks, process image and clean up temporary files"""
    upload_id = None
    temp_merged_path = None
    backup_path = None
    
    try:
        upload_id = request.form.get('uploadId')
        
        if not upload_id:
            return jsonify({'success': False, 'message': 'Invalid upload ID'}), 400
        
        upload_dir = os.path.join(TEMP_CHUNKS_FOLDER, upload_id)
        if not os.path.exists(upload_dir):
            return jsonify({'success': False, 'message': 'Upload session does not exist or has expired'}), 404
        
        # 读取信息文件
        info_file = os.path.join(upload_dir, 'info.json')
        with open(info_file, 'r') as f:
            info = json.load(f)
        
        # 检查上传是否完整
        if info['receivedChunks'] < info['totalChunks']:
            return jsonify({'success': False, 'message': 'Upload incomplete, some chunks are missing'}), 400
        
        original_filename = info['originalFilename']
        secure_name = secure_filename(original_filename)
        
        # 合并分块
        temp_merged_path = os.path.join(UPLOAD_FOLDER, f"temp_{secure_name}")
        os.makedirs(os.path.dirname(temp_merged_path), exist_ok=True)
        
        logging.info(f"开始合并分块: {upload_id}, 文件名: {original_filename}")
        
        with open(temp_merged_path, 'wb') as merged_file:
            for i in range(info['totalChunks']):
                chunk_path = os.path.join(upload_dir, f'chunk_{i:06d}')
                if os.path.exists(chunk_path):
                    with open(chunk_path, 'rb') as chunk_file:
                        merged_file.write(chunk_file.read())
                else:
                    logging.error(f"无法找到分块: {chunk_path}")
                    return jsonify({'success': False, 'message': f'Chunk {i} is missing'}), 400
        
        # 确保照片目录存在
        os.makedirs(PHOTOS_FOLDER, exist_ok=True)
        target_path = os.path.join(PHOTOS_FOLDER, secure_name)
        
        # 创建备份
        if os.path.exists(target_path):
            backup_path = target_path + '.backup'
            try:
                shutil.copy2(target_path, backup_path)
                logging.info(f"创建文件备份: {os.path.basename(target_path)} -> {os.path.basename(backup_path)}")
            except Exception as backup_error:
                logging.error(f"创建备份失败: {backup_error}")
                # 继续处理，即使备份失败
        
        # 优化图片并保存到最终位置
        logging.info(f"开始优化图片: {temp_merged_path} -> {target_path}")
        try:
            compress_result = optimize_image(temp_merged_path, target_path, preserve_size=True)
            logging.info(f"图片压缩完成，压缩率: {compress_result['compression_ratio']:.1f}%")
            
            # 确保缩略图目录存在
            os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)
            
            # 生成缩略图
            cache_filename = get_cache_filename(secure_name, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
            cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
            generate_thumbnail(target_path, cache_path)
            logging.info(f"缩略图生成完成: {cache_path}")
            
        except Exception as img_error:
            logging.error(f"处理图片时出错: {img_error}")
            # 如果处理失败，使用原始文件
            try:
                if os.path.exists(temp_merged_path) and not os.path.exists(target_path):
                    shutil.copy2(temp_merged_path, target_path)
                    logging.info(f"处理失败，使用原始文件: {temp_merged_path} -> {target_path}")
                    
                    # 生成缩略图
                    cache_filename = get_cache_filename(secure_name, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
                    cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
                    generate_thumbnail(target_path, cache_path)
                    logging.info(f"使用原始文件创建缩略图: {cache_path}")
            except Exception as fallback_error:
                logging.error(f"使用原始文件失败: {fallback_error}")
                # 恢复备份
                if backup_path and os.path.exists(backup_path):
                    try:
                        shutil.copy2(backup_path, target_path)
                        logging.info(f"从备份恢复: {backup_path} -> {target_path}")
                    except Exception as restore_error:
                        logging.error(f"恢复备份失败: {restore_error}")
                        return jsonify({'success': False, 'message': f'Image processing failed and backup restore failed: {str(restore_error)}'}), 500
                else:
                    return jsonify({'success': False, 'message': f'Image processing failed and no backup available: {str(fallback_error)}'}), 500
        
        # 清理临时文件和目录
        try:
            # 删除临时合并文件
            if temp_merged_path and os.path.exists(temp_merged_path):
                os.remove(temp_merged_path)
                logging.info(f"删除临时合并文件: {temp_merged_path}")
            
            # 删除临时目录
            if upload_dir and os.path.exists(upload_dir):
                shutil.rmtree(upload_dir)
                logging.info(f"删除临时上传目录: {upload_dir}")
            
            # 删除备份
            if backup_path and os.path.exists(backup_path):
                os.remove(backup_path)
                logging.info(f"删除备份文件: {backup_path}")
                
            # 额外清理所有临时文件
            clean_temp_files()
        except Exception as cleanup_error:
            logging.error(f"清理临时文件时出错: {cleanup_error}")
            # 继续处理，即使清理失败
        
        # 返回成功
        return jsonify({
            'success': True,
            'filename': secure_name,
            'message': 'Upload completed and image processed successfully'
        })
    
    except Exception as e:
        logging.error(f"处理上传时出错: {e}")
        # 确保错误时也清理临时文件
        try:
            if temp_merged_path and os.path.exists(temp_merged_path):
                os.remove(temp_merged_path)
            
            if upload_id:
                upload_dir = os.path.join(TEMP_CHUNKS_FOLDER, upload_id)
                if os.path.exists(upload_dir):
                    shutil.rmtree(upload_dir)
            
            if backup_path and os.path.exists(backup_path):
                os.remove(backup_path)
                
            # 错误情况下也清理临时文件
            clean_temp_files()
        except Exception as error_cleanup_error:
            logging.error(f"错误处理中的清理失败: {error_cleanup_error}")
            
        return jsonify({'success': False, 'message': f'Failed to complete upload: {str(e)}'}), 500

# 清理临时文件
def clean_temp_files():
    """
    清理所有临时文件夹中的孤立文件和过期文件
    - 删除TEMP_CHUNKS_FOLDER中超过24小时的文件和文件夹
    - 删除PHOTOS_FOLDER中的.temp和.backup文件
    - 删除UPLOAD_FOLDER中超过24小时的文件
    
    返回: 删除的文件总数
    """
    total_deleted = 0
    try:
        logging.info("开始清理临时文件...")
        now = time.time()
        expiration_time = 24 * 60 * 60  # 24小时
        
        # 清理临时分块目录
        if os.path.exists(TEMP_CHUNKS_FOLDER):
            deleted_count = 0
            for item in os.listdir(TEMP_CHUNKS_FOLDER):
                item_path = os.path.join(TEMP_CHUNKS_FOLDER, item)
                try:
                    # 获取文件/目录的最后修改时间
                    mtime = os.path.getmtime(item_path)
                    # 如果超过24小时
                    if now - mtime > expiration_time:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                        else:
                            os.remove(item_path)
                        deleted_count += 1
                        logging.info(f"删除过期临时文件/目录: {item_path}")
                except Exception as e:
                    logging.error(f"清理临时文件时出错: {e}")
            
            if deleted_count > 0:
                logging.info(f"已清理 {deleted_count} 个过期临时文件/目录")
                total_deleted += deleted_count
        
        # 清理照片目录中的临时文件
        if os.path.exists(PHOTOS_FOLDER):
            temp_deleted = 0
            for root, _, files in os.walk(PHOTOS_FOLDER):
                for file in files:
                    if file.endswith(".temp") or file.endswith(".backup"):
                        try:
                            file_path = os.path.join(root, file)
                            os.remove(file_path)
                            temp_deleted += 1
                            logging.info(f"删除照片目录中的临时文件: {file_path}")
                        except Exception as e:
                            logging.error(f"删除照片目录临时文件时出错: {e}")
            
            if temp_deleted > 0:
                logging.info(f"已清理照片目录中的 {temp_deleted} 个临时文件")
                total_deleted += temp_deleted
        
        # 清理上传目录中的过期文件
        if os.path.exists(UPLOAD_FOLDER):
            upload_deleted = 0
            for file in os.listdir(UPLOAD_FOLDER):
                file_path = os.path.join(UPLOAD_FOLDER, file)
                if os.path.isfile(file_path):
                    try:
                        # 获取文件的最后修改时间
                        mtime = os.path.getmtime(file_path)
                        # 如果超过24小时
                        if now - mtime > expiration_time:
                            os.remove(file_path)
                            upload_deleted += 1
                            logging.info(f"删除过期上传文件: {file_path}")
                    except Exception as e:
                        logging.error(f"删除上传目录文件时出错: {e}")
            
            if upload_deleted > 0:
                logging.info(f"已清理上传目录中的 {upload_deleted} 个过期文件")
                total_deleted += upload_deleted
                
        logging.info(f"临时文件清理完成，共删除 {total_deleted} 个文件")
    except Exception as e:
        logging.error(f"清理临时文件过程中出错: {e}")
        
    return total_deleted

# 添加定期清理临时文件的功能
@app.before_request
def startup():
    global startup_complete
    if not getattr(app, 'startup_complete', False):
        # 应用启动时清理一次临时文件
        clean_temp_files()
        
        # 启动定期清理任务
        if not hasattr(app, 'cleanup_thread'):
            def scheduled_cleanup():
                while True:
                    try:
                        # 每天清理一次临时文件
                        time.sleep(24 * 60 * 60)  # 24小时
                        clean_temp_files()
                    except Exception as e:
                        logging.error(f"定期清理任务出错: {e}")
            
            app.cleanup_thread = threading.Thread(target=scheduled_cleanup, daemon=True)
            app.cleanup_thread.start()
            logging.info("已启动定期清理临时文件任务")
        
        app.startup_complete = True
        logging.info("应用程序启动...")

@app.route('/')
def index():
    photos = get_photos()
    current_year = datetime.datetime.now().year
    return render_template('index.html', photos=photos, current_year=current_year)

@app.route('/photos/<filename>')
def get_photo(filename):
    return send_from_directory(PHOTOS_FOLDER, filename)

@app.route('/thumbnails/<filename>')
def get_thumbnail(filename):
    return send_from_directory(THUMBNAIL_FOLDER, filename)

@app.route('/api/photos')
def api_photos():
    """API端点，返回所有照片的JSON数据"""
    photos = get_photos()
    return jsonify(photos)

# 修改缓存状态API，移除监控相关内容
@app.route('/api/cache/status')
def api_cache_status():
    """返回缓存状态信息"""
    # 计算照片总数和缓存总数
    photo_count = 0
    for filename in os.listdir(PHOTOS_FOLDER):
        if os.path.isfile(os.path.join(PHOTOS_FOLDER, filename)) and \
           os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS:
            photo_count += 1
    
    cache_count = 0
    if os.path.exists(THUMBNAIL_FOLDER):
        cache_count = len(os.listdir(THUMBNAIL_FOLDER))
    
    return jsonify({
        "photo_count": photo_count,
        "cache_count": cache_count,
        "cache_folder": THUMBNAIL_FOLDER
    })

# 禁用缓存监控API
@app.route('/api/cache/monitor/<action>', methods=['POST'])
@login_required
def api_cache_monitor(action):
    """启动或停止缓存监控(已禁用)"""
    return jsonify({"status": "disabled", "message": "缓存监控功能已禁用"})

# 传统表单上传（备用方案）
@app.route('/admin/upload', methods=['POST'])
@login_required
def upload_photos():
    """Traditional form upload method (as backup)"""
    if 'photos' not in request.files:
        flash('No files selected', 'error')
        return redirect(url_for('admin'))
    
    files = request.files.getlist('photos')
    
    if not files or files[0].filename == '':
        flash('No files selected', 'error')
        return redirect(url_for('admin'))
    
    uploaded_count = 0
    error_count = 0
    for file in files:
        if file:
            filename = secure_filename(file.filename)
            ext = os.path.splitext(filename)[1].lower()
            
            if ext not in ALLOWED_EXTENSIONS:
                logging.warning(f"不支持的文件格式: {filename}")
                error_count += 1
                continue
            
            try:
                # 确保上传文件夹存在
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                
                # 临时保存上传的文件
                temp_path = os.path.join(UPLOAD_FOLDER, filename)
                file.save(temp_path)
                
                if not os.path.exists(temp_path):
                    logging.error(f"保存临时文件失败: {filename}")
                    error_count += 1
                    continue
                
                # 确保照片文件夹存在
                os.makedirs(PHOTOS_FOLDER, exist_ok=True)
                
                # 优化图片（压缩但不损失画质）
                target_path = os.path.join(PHOTOS_FOLDER, filename)
                
                try:
                    # 压缩图片（保持原始尺寸）
                    optimize_image(temp_path, target_path, preserve_size=True)
                    
                    # 确保缩略图文件夹存在
                    os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)
                    
                    # 生成缩略图
                    cache_filename = get_cache_filename(filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
                    cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
                    
                    if os.path.exists(target_path):
                        generate_thumbnail(target_path, cache_path)
                        uploaded_count += 1
                        logging.info(f"成功上传并处理图片: {filename}")
                    else:
                        logging.error(f"优化后的图片不存在: {target_path}")
                        # 尝试直接复制
                        shutil.copy2(temp_path, target_path)
                        generate_thumbnail(target_path, cache_path)
                        uploaded_count += 1
                        logging.info(f"优化失败，已直接复制原图: {filename}")
                except Exception as process_error:
                    logging.error(f"处理图片时出错: {process_error}")
                    error_count += 1
                    # 尝试直接复制
                    try:
                        shutil.copy2(temp_path, target_path)
                        generate_thumbnail(target_path, cache_path)
                        uploaded_count += 1
                        logging.info(f"处理失败，已直接复制原图: {filename}")
                    except Exception as copy_error:
                        logging.error(f"直接复制失败: {copy_error}")
                        error_count += 1
            except Exception as e:
                logging.error(f"上传过程出错: {e}")
                error_count += 1
            finally:
                # 删除临时文件
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as remove_error:
                        logging.error(f"删除临时文件失败: {remove_error}")
    
    if uploaded_count > 0:
        if error_count > 0:
            flash(f'Successfully uploaded {uploaded_count} images with {error_count} errors', 'warning')
        else:
            flash(f'Successfully uploaded {uploaded_count} images', 'success')
    else:
        flash('Image upload failed', 'error')
    
    return redirect(url_for('admin'))

# 修改照片名称
@app.route('/admin/rename', methods=['POST'])
@login_required
def rename_photo():
    old_filename = request.form.get('old_filename')
    new_name = request.form.get('new_filename', '').strip() # 使用new_filename而不是new_name
    
    if not old_filename:
        return jsonify({'success': False, 'message': 'Incomplete parameters: missing original filename'}), 400
    
    if not new_name:
        return jsonify({'success': False, 'message': 'New filename cannot be empty'}), 400
    
    try:
        # 确保文件夹存在
        if not os.path.exists(PHOTOS_FOLDER):
            os.makedirs(PHOTOS_FOLDER, exist_ok=True)
            logging.info(f"创建照片文件夹: {PHOTOS_FOLDER}")
        
        if not os.path.exists(THUMBNAIL_FOLDER):
            os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)
            logging.info(f"创建缩略图文件夹: {THUMBNAIL_FOLDER}")
        
        # 获取原文件路径和扩展名
        old_path = os.path.join(PHOTOS_FOLDER, old_filename)
        if not os.path.exists(old_path):
            return jsonify({'success': False, 'message': 'File does not exist'}), 404
        
        # 生成新文件名（保留原扩展名）
        _, ext = os.path.splitext(old_filename)
        
        # 确保扩展名有效，防止只有扩展名的情况
        if not ext:
            return jsonify({'success': False, 'message': 'Original file has no valid extension'}), 400
        
        # 使用secure_filename确保文件名安全
        new_filename = secure_filename(new_name + ext)
        
        # 额外检查：确保新文件名不只是扩展名
        if new_filename == ext or new_filename.startswith('.'):
            return jsonify({'success': False, 'message': 'Invalid new filename, filename cannot be empty'}), 400
        
        new_path = os.path.join(PHOTOS_FOLDER, new_filename)
        
        # 检查新文件名是否已存在
        if os.path.exists(new_path) and old_filename != new_filename:
            return jsonify({'success': False, 'message': 'Filename already exists'}), 400
        
        # 创建临时备份文件
        backup_path = old_path + '.bak'
        try:
            shutil.copy2(old_path, backup_path)
            logging.info(f"创建文件备份: {os.path.basename(old_path)} -> {os.path.basename(backup_path)}")
        except Exception as backup_error:
            logging.error(f"创建备份文件失败: {backup_error}")
            return jsonify({'success': False, 'message': f'Failed to create backup: {str(backup_error)}'}), 500
        
        try:
            # 重命名文件
            os.rename(old_path, new_path)
            logging.info(f"重命名文件: {old_filename} -> {new_filename}")
            
            # 处理缩略图缓存
            old_cache_filename = get_cache_filename(old_filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
            old_cache_path = os.path.join(THUMBNAIL_FOLDER, old_cache_filename)
            
            new_cache_filename = get_cache_filename(new_filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
            new_cache_path = os.path.join(THUMBNAIL_FOLDER, new_cache_filename)
            
            # 删除旧缓存
            if os.path.exists(old_cache_path):
                try:
                    os.remove(old_cache_path)
                    logging.info(f"删除旧缓存: {old_cache_filename}")
                except Exception as cache_error:
                    logging.error(f"删除旧缓存失败: {cache_error}")
                    # 继续处理，不影响主流程
            
            # 为新文件生成缩略图
            try:
                generate_thumbnail(new_path, new_cache_path)
                logging.info(f"为重命名文件生成新缩略图: {new_filename}")
            except Exception as thumb_error:
                logging.error(f"生成新缩略图失败: {thumb_error}")
                # 继续处理，不影响主流程
            
            # 操作成功，删除备份
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                    logging.info(f"删除备份文件: {os.path.basename(backup_path)}")
                except Exception as remove_error:
                    logging.error(f"删除备份失败: {remove_error}")
                    # 继续处理，不影响主流程
            
            return jsonify({'success': True, 'message': 'Renamed successfully', 'new_filename': new_filename})
        except Exception as e:
            # 如果重命名过程中出错，恢复备份
            logging.error(f"重命名文件时出错: {e}")
            if os.path.exists(backup_path):
                try:
                    if os.path.exists(new_path):
                        os.remove(new_path)  # 删除可能部分创建的新文件
                        logging.info(f"删除部分创建的新文件: {new_filename}")
                    
                    shutil.copy2(backup_path, old_path)  # 恢复原文件
                    logging.info(f"从备份恢复原文件: {old_filename}")
                    
                    os.remove(backup_path)  # 删除备份
                    logging.info(f"删除备份文件: {os.path.basename(backup_path)}")
                except Exception as restore_error:
                    logging.error(f"恢复备份失败: {restore_error}")
            raise e  # 重新抛出异常让外层捕获
    except Exception as e:
        logging.error(f"重命名图片时出错: {e}")
        return jsonify({'success': False, 'message': f'Rename failed: {str(e)}'}), 500

# 删除照片
@app.route('/admin/delete', methods=['POST'])
@login_required
def delete_photo():
    filename = request.form.get('filename')
    
    if not filename:
        return jsonify({'success': False, 'message': 'Incomplete parameters'}), 400
    
    try:
        # 创建原图的备份
        file_path = os.path.join(PHOTOS_FOLDER, filename)
        backup_path = file_path + '.bak'
        
        if os.path.exists(file_path):
            try:
                # 创建备份，以防删除后需要恢复
                shutil.copy2(file_path, backup_path)
                logging.info(f"创建删除前备份: {filename}")
            except Exception as backup_error:
                logging.error(f"创建删除备份失败: {backup_error}")
                # 继续处理，不强制要求有备份
            
            try:
                # 删除原图
                os.remove(file_path)
                logging.info(f"删除原图: {filename}")
            except Exception as del_error:
                logging.error(f"删除原图失败: {del_error}")
                # 删除失败，恢复原始状态
                if os.path.exists(backup_path):
                    os.remove(backup_path)  # 删除备份
                return jsonify({'success': False, 'message': f'Failed to delete original image: {str(del_error)}'}), 500
        else:
            logging.warning(f"要删除的文件不存在: {filename}")
        
        # 删除缓存
        cache_filename = get_cache_filename(filename, THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE)
        cache_path = os.path.join(THUMBNAIL_FOLDER, cache_filename)
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                logging.info(f"删除缩略图缓存: {cache_filename}")
            except Exception as cache_error:
                logging.error(f"删除缩略图缓存失败: {cache_error}")
                # 缓存删除失败不影响整体操作
        
        # 删除成功，移除备份
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
                logging.info(f"删除备份文件: {os.path.basename(backup_path)}")
            except Exception as remove_error:
                logging.error(f"删除备份失败: {remove_error}")
                # 备份删除失败不影响整体操作
        
        return jsonify({'success': True, 'message': 'Successfully deleted'})
    except Exception as e:
        logging.error(f"删除图片时出错: {e}")
        
        # 删除失败，恢复原始文件
        if 'backup_path' in locals() and os.path.exists(backup_path) and not os.path.exists(file_path):
            try:
                shutil.copy2(backup_path, file_path)
                logging.info(f"从备份恢复原图: {filename}")
                os.remove(backup_path)  # 删除备份
            except Exception as restore_error:
                logging.error(f"恢复原图失败: {restore_error}")
        
        return jsonify({'success': False, 'message': f'Delete failed: {str(e)}'}), 500

# 预处理所有图片并生成缓存
def preprocess_all_photos():
    # 已禁用，保留空函数以防被引用
    pass

# 监视照片文件夹的函数，在单独的线程中运行
def watch_photos_folder(interval=300):  # 默认5分钟检查一次
    """监视照片文件夹的变化，定期更新缓存 (已禁用)"""
    # 已禁用，保留空函数以防被引用
    pass

# 缓存监控线程
cache_monitor_thread = None

# 启动缓存监控线程的函数
def start_cache_monitor(interval=300):
    # 已禁用，保留空函数以防被引用
    pass

# 分块上传 - 上传状态
@app.route('/admin/upload/status/<upload_id>', methods=['GET'])
@login_required
def upload_status(upload_id):
    """Get upload status"""
    try:
        if not upload_id:
            return jsonify({'success': False, 'message': 'Invalid upload ID'}), 400
        
        upload_dir = os.path.join(TEMP_CHUNKS_FOLDER, upload_id)
        if not os.path.exists(upload_dir):
            return jsonify({'success': False, 'message': 'Upload session does not exist or has expired'}), 404
        
        # 读取信息文件
        info_file = os.path.join(upload_dir, 'info.json')
        with open(info_file, 'r') as f:
            info = json.load(f)
        
        # 计算进度
        progress = int((info['receivedChunks'] / info['totalChunks']) * 100) if info['totalChunks'] > 0 else 0
        
        return jsonify({
            'success': True,
            'status': info['status'],
            'received': info['receivedChunks'],
            'total': info['totalChunks'],
            'progress': progress,
            'filename': info['originalFilename']
        })
    except Exception as e:
        logging.error(f"Error getting upload status: {e}")
        return jsonify({'success': False, 'message': f'Failed to get status: {str(e)}'}), 500

# 分块上传 - 清理临时文件
@app.route('/admin/upload/cleanup', methods=['POST'])
@login_required
def cleanup_temp_files():
    """手动触发清理临时文件，确保上传完成后不留下临时文件"""
    try:
        logging.info("手动触发临时文件清理")
        # 清理临时文件
        deleted_files = clean_temp_files()
        
        # 额外检查确保文件夹干净
        extra_deleted = 0
        
        # 检查temp_chunks文件夹
        if os.path.exists(TEMP_CHUNKS_FOLDER):
            for item in os.listdir(TEMP_CHUNKS_FOLDER):
                item_path = os.path.join(TEMP_CHUNKS_FOLDER, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                        extra_deleted += 1
                        logging.info(f"删除临时上传目录: {item_path}")
                    elif os.path.isfile(item_path):
                        os.remove(item_path)
                        extra_deleted += 1
                        logging.info(f"删除临时文件: {item_path}")
                except Exception as e:
                    logging.error(f"删除 {item_path} 时出错: {e}")
        
        # 检查上传文件夹中的临时文件
        for folder in [UPLOAD_FOLDER, PHOTOS_FOLDER]:
            if os.path.exists(folder):
                for file in os.listdir(folder):
                    if file.endswith('.temp') or file.endswith('.bak') or file.endswith('.backup') or '.temp.' in file:
                        try:
                            file_path = os.path.join(folder, file)
                            os.remove(file_path)
                            extra_deleted += 1
                            logging.info(f"删除临时文件: {file_path}")
                        except Exception as e:
                            logging.error(f"删除 {file_path} 时出错: {e}")

        return jsonify({
            'success': True,
            'message': '临时文件清理完成',
            'deleted_count': deleted_files + extra_deleted
        })
    except Exception as e:
        logging.error(f"清理临时文件时出错: {e}")
        return jsonify({
            'success': False,
            'message': f'清理临时文件时出错: {str(e)}'
        }), 500

if __name__ == '__main__':
    logging.info("应用程序启动...")
    
    # 直接启动应用
    app.run(debug=True, host='0.0.0.0',port=5001) 