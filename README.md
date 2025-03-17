# 个人摄影作品集

这是一个基于Flask的个人摄影作品集展示网站，使用Tailwind CSS构建美观的用户界面。

## 功能特点

- 响应式设计，适配各种设备
- 作品集按照分类组织
- 缩略图模式显示，减少网络加载
- 点击缩略图查看原图
- 优雅的图片懒加载

## 安装与运行

1. 安装依赖：
   ```
   pip install -r requirements.txt
   ```

2. 运行应用：
   ```
   python app.py
   ```

3. 访问网站：
   在浏览器中打开 http://localhost:5000

## 使用方法

将您的照片放入photos文件夹中即可自动加载到网站中。支持的图片格式包括：
- JPG/JPEG
- PNG
- GIF

## 项目结构

```
.
├── app.py              # Flask应用主文件
├── photos/             # 存放照片的文件夹
├── requirements.txt    # 项目依赖
├── static/             # 静态资源文件夹
│   ├── css/            # CSS样式文件
│   └── js/             # JavaScript文件
└── templates/          # HTML模板
    └── index.html      # 主页模板
```

## 技术栈

- Flask: 轻量级Python Web框架
- Pillow: 图像处理库，用于生成缩略图
- Tailwind CSS: 实用优先的CSS框架，用于构建响应式界面

## 自定义

您可以通过修改以下文件来自定义网站：
- `templates/index.html`: 修改网站布局和样式
- `app.py`: 调整缩略图大小或添加新功能
